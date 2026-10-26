
import hashlib
import secrets
from datetime import datetime, timezone, timedelta

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, EmailStr
from pymongo.errors import DuplicateKeyError

from app.database import users_collection
from app.schemas import RegisterRequest, LoginRequest, PhoneUpdateRequest
from app.security import (
    hash_password,
    verify_password,
    create_access_token,
    decode_access_token,
)
from app.email_service import send_verification_otp

router = APIRouter(prefix="/api/auth", tags=["Authentication"])
bearer = HTTPBearer(auto_error=False)

OTP_EXPIRY_MINUTES = 10
OTP_RESEND_SECONDS = 60
OTP_MAX_ATTEMPTS = 5


class ProfileUpdateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    phone: str | None = Field(default=None, max_length=16)


class VerifyEmailRequest(BaseModel):
    email: EmailStr
    otp: str = Field(pattern=r"^\d{6}$")


class ResendOtpRequest(BaseModel):
    email: EmailStr


def otp_hash(otp: str) -> str:
    return hashlib.sha256(otp.encode()).hexdigest()


def public_user(user: dict) -> dict:
    return {
        "id": str(user["_id"]),
        "name": user["name"],
        "email": user["email"],
        "phone": user.get("phone"),
        "role": user["role"],
        "email_verified": user.get("email_verified", False),
    }


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
):
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = decode_access_token(credentials.credentials)
        user_id = payload.get("sub")

        if not user_id or not ObjectId.is_valid(user_id):
            raise ValueError("Invalid user ID")
    except Exception:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = await users_collection.find_one({"_id": ObjectId(user_id)})

    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    if not user.get("email_verified", False):
        raise HTTPException(status_code=403, detail="Please verify your email")

    return user


@router.post("/register", status_code=201)
async def register(data: RegisterRequest):
    email = str(data.email).lower().strip()
    existing = await users_collection.find_one({"email": email})

    if existing and existing.get("email_verified", False):
        raise HTTPException(
            status_code=409,
            detail="An account with this email already exists",
        )

    now = datetime.now(timezone.utc)
    otp = f"{secrets.randbelow(1_000_000):06d}"

    user_data = {
        "name": data.name.strip(),
        "email": email,
        "phone": data.phone,
        "password_hash": hash_password(data.password),
        "role": data.role,
        "email_verified": False,
        "email_otp_hash": otp_hash(otp),
        "email_otp_expires_at": now + timedelta(minutes=OTP_EXPIRY_MINUTES),
        "email_otp_last_sent_at": now,
        "email_otp_attempts": 0,
        "created_at": existing.get("created_at", now) if existing else now,
    }

    if existing:
        # Refresh only an unverified account.
        await users_collection.update_one(
            {"_id": existing["_id"]},
            {"$set": user_data},
        )
        user_id = existing["_id"]
    else:
        try:
            result = await users_collection.insert_one(user_data)
            user_id = result.inserted_id
        except DuplicateKeyError:
            raise HTTPException(
                status_code=409,
                detail="An account with this email already exists",
            )

    try:
        send_verification_otp(email, otp)
    except Exception:
        # Never return a login token before email verification.
        raise HTTPException(
            status_code=502,
            detail="Could not send verification email. Please try again.",
        )

    return {
        "message": "Verification code sent to your email",
        "email": email,
        "expires_in_seconds": OTP_EXPIRY_MINUTES * 60,
    }


@router.post("/verify-email")
async def verify_email(data: VerifyEmailRequest):
    email = str(data.email).lower().strip()
    user = await users_collection.find_one({"email": email})

    if not user:
        raise HTTPException(status_code=400, detail="Invalid email or OTP")

    if user.get("email_verified", False):
        return {"message": "Email already verified. You can log in."}

    now = datetime.now(timezone.utc)
    expires_at = user.get("email_otp_expires_at")

    # MongoDB may return a naive datetime depending on its configuration.
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if user.get("email_otp_attempts", 0) >= OTP_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Request a new code.",
        )

    if not expires_at or expires_at <= now:
        raise HTTPException(
            status_code=400,
            detail="OTP expired. Request a new code.",
        )

    supplied_hash = otp_hash(data.otp)

    if not secrets.compare_digest(
        supplied_hash,
        user.get("email_otp_hash", ""),
    ):
        await users_collection.update_one(
            {"_id": user["_id"]},
            {"$inc": {"email_otp_attempts": 1}},
        )
        raise HTTPException(status_code=400, detail="Invalid email or OTP")

    await users_collection.update_one(
        {"_id": user["_id"]},
        {
            "$set": {"email_verified": True},
            "$unset": {
                "email_otp_hash": "",
                "email_otp_expires_at": "",
                "email_otp_last_sent_at": "",
                "email_otp_attempts": "",
            },
        },
    )

    return {
        "message": "Email verified successfully. You can now log in."
    }


@router.post("/resend-otp")
async def resend_otp(data: ResendOtpRequest):
    email = str(data.email).lower().strip()
    user = await users_collection.find_one({"email": email})

    # Avoid disclosing account status.
    if not user or user.get("email_verified", False):
        return {
            "message": "If eligible, a verification code will be sent."
        }

    now = datetime.now(timezone.utc)
    last_sent = user.get("email_otp_last_sent_at")

    if last_sent and last_sent.tzinfo is None:
        last_sent = last_sent.replace(tzinfo=timezone.utc)

    if last_sent and (now - last_sent).total_seconds() < OTP_RESEND_SECONDS:
        raise HTTPException(
            status_code=429,
            detail="Please wait before requesting another code.",
        )

    otp = f"{secrets.randbelow(1_000_000):06d}"

    # Update the stored OTP only after email sending succeeds.
    try:
        send_verification_otp(email, otp)
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="Could not send verification email. Please try again later.",
        )

    await users_collection.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "email_otp_hash": otp_hash(otp),
                "email_otp_expires_at": now + timedelta(minutes=OTP_EXPIRY_MINUTES),
                "email_otp_last_sent_at": now,
                "email_otp_attempts": 0,
            }
        },
    )

    return {
        "message": "If eligible, a verification code will be sent.",
        "expires_in_seconds": OTP_EXPIRY_MINUTES * 60,
    }


@router.post("/login")
async def login(data: LoginRequest):
    email = str(data.email).lower().strip()
    user = await users_collection.find_one({"email": email})

    if not user or not verify_password(
        data.password, user["password_hash"]
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password",
        )

    if not user.get("email_verified", False):
        raise HTTPException(
            status_code=403,
            detail="Please verify your email before logging in.",
        )

    token = create_access_token(str(user["_id"]), user["role"])

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": public_user(user),
    }


@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    return public_user(user)


@router.patch("/profile")
async def update_profile(
    data: ProfileUpdateRequest,
    user: dict = Depends(get_current_user),
):
    name = data.name.strip()

    if len(name) < 2:
        raise HTTPException(
            status_code=422,
            detail="Name must contain at least 2 characters",
        )

    phone = (data.phone or "").strip().replace(" ", "").replace("-", "") or None

    if phone and not (
        (
            len(phone) == 10
            and phone.isdigit()
            and phone[0] in "6789"
        )
        or (
            len(phone) == 13
            and phone.startswith("+91")
            and phone[3] in "6789"
            and phone[4:].isdigit()
        )
    ):
        raise HTTPException(
            status_code=422,
            detail="Enter a valid Indian mobile number or leave it blank",
        )

    await users_collection.update_one(
        {"_id": user["_id"]},
        {"$set": {"name": name, "phone": phone}},
    )

    user["name"] = name
    user["phone"] = phone

    return {
        "message": "Profile updated successfully",
        "user": public_user(user),
    }


@router.patch("/phone")
async def update_phone(
    data: PhoneUpdateRequest,
    user: dict = Depends(get_current_user),
):
    await users_collection.update_one(
        {"_id": user["_id"]},
        {"$set": {"phone": data.phone}},
    )

    user["phone"] = data.phone

    return {
        "message": "Phone number updated successfully",
        "user": public_user(user),
    }
