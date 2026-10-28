import hashlib
import os
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
from app.email_service import (
    send_verification_otp,
    send_password_reset_email,
)

router = APIRouter(prefix="/api/auth", tags=["Authentication"])
bearer = HTTPBearer(auto_error=False)

OTP_EXPIRY_MINUTES = 10
OTP_RESEND_SECONDS = 60
OTP_MAX_ATTEMPTS = 5
PASSWORD_RESET_EXPIRY_MINUTES = 30
PASSWORD_RESET_RESEND_SECONDS = 60


# =========================
# REQUEST MODELS
# =========================

class ProfileUpdateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    phone: str | None = Field(default=None, max_length=16)


class VerifyEmailRequest(BaseModel):
    email: EmailStr
    otp: str = Field(pattern=r"^\d{6}$")


class ResendOtpRequest(BaseModel):
    email: EmailStr


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=32, max_length=200)
    new_password: str = Field(min_length=8, max_length=128)


# =========================
# HELPERS
# =========================

def otp_hash(otp: str) -> str:
    return hashlib.sha256(otp.encode("utf-8")).hexdigest()


def reset_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_email(email: str) -> str:
    return email.lower().strip()


def public_user(user: dict) -> dict:
    return {
        "id": str(user["_id"]),
        "name": user["name"],
        "email": user["email"],
        "phone": user.get("phone"),
        "role": user["role"],
        "email_verified": user.get("email_verified", False),
    }


def normalize_datetime(value):
    if value and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# =========================
# AUTHENTICATION DEPENDENCY
# =========================

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
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = await users_collection.find_one({"_id": ObjectId(user_id)})

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    if not user.get("email_verified", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Please verify your email",
        )

    return user


# =========================
# REGISTER
# =========================

@router.post("/register", status_code=201)
async def register(data: RegisterRequest):
    email = normalize_email(str(data.email))

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
        "email_otp_expires_at": now + timedelta(
            minutes=OTP_EXPIRY_MINUTES
        ),
        "email_otp_last_sent_at": now,
        "email_otp_attempts": 0,
        "created_at": existing.get("created_at", now) if existing else now,
    }

    if existing:
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
        raise HTTPException(
            status_code=502,
            detail="Could not send verification email. Please try again.",
        )

    return {
        "message": "Verification code sent to your email",
        "email": email,
        "expires_in_seconds": OTP_EXPIRY_MINUTES * 60,
    }


# =========================
# VERIFY EMAIL OTP
# =========================

@router.post("/verify-email")
async def verify_email(data: VerifyEmailRequest):
    email = normalize_email(str(data.email))
    user = await users_collection.find_one({"email": email})

    if not user:
        raise HTTPException(
            status_code=400,
            detail="Invalid email or OTP",
        )

    if user.get("email_verified", False):
        return {
            "message": "Email already verified. You can log in."
        }

    now = datetime.now(timezone.utc)
    expires_at = normalize_datetime(
        user.get("email_otp_expires_at")
    )

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
        raise HTTPException(
            status_code=400,
            detail="Invalid email or OTP",
        )

    await users_collection.update_one(
        {
            "_id": user["_id"],
            "email_verified": False,
            "email_otp_hash": supplied_hash,
            "email_otp_expires_at": {"$gt": now},
            "email_otp_attempts": {"$lt": OTP_MAX_ATTEMPTS},
        },
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

    verified_user = await users_collection.find_one(
        {"_id": user["_id"]}
    )

    if not verified_user or not verified_user.get("email_verified"):
        raise HTTPException(
            status_code=400,
            detail="Could not verify email. Request a new code.",
        )

    return {
        "message": "Email verified successfully. You can now log in."
    }


# =========================
# RESEND EMAIL OTP
# =========================

@router.post("/resend-otp")
async def resend_otp(data: ResendOtpRequest):
    email = normalize_email(str(data.email))
    user = await users_collection.find_one({"email": email})

    if not user or user.get("email_verified", False):
        return {
            "message": "If eligible, a verification code will be sent."
        }

    now = datetime.now(timezone.utc)
    last_sent = normalize_datetime(
        user.get("email_otp_last_sent_at")
    )

    if (
        last_sent
        and (now - last_sent).total_seconds() < OTP_RESEND_SECONDS
    ):
        raise HTTPException(
            status_code=429,
            detail="Please wait before requesting another code.",
        )

    otp = f"{secrets.randbelow(1_000_000):06d}"

    try:
        send_verification_otp(email, otp)
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="Could not send verification email. Please try again later.",
        )

    await users_collection.update_one(
        {"_id": user["_id"], "email_verified": False},
        {
            "$set": {
                "email_otp_hash": otp_hash(otp),
                "email_otp_expires_at": now + timedelta(
                    minutes=OTP_EXPIRY_MINUTES
                ),
                "email_otp_last_sent_at": now,
                "email_otp_attempts": 0,
            }
        },
    )

    return {
        "message": "If eligible, a verification code will be sent.",
        "expires_in_seconds": OTP_EXPIRY_MINUTES * 60,
    }


# =========================
# LOGIN
# =========================

@router.post("/login")
async def login(data: LoginRequest):
    email = normalize_email(str(data.email))
    user = await users_collection.find_one({"email": email})

    if not user or not verify_password(
        data.password,
        user["password_hash"],
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


# =========================
# FORGOT PASSWORD
# =========================

@router.post("/forgot-password")
async def forgot_password(data: ForgotPasswordRequest):
    email = normalize_email(str(data.email))

    # Keep the response generic to avoid disclosing account existence.
    generic_message = (
        "If an account exists for that email, "
        "a password reset link will be sent."
    )

    user = await users_collection.find_one({"email": email})

    if not user or not user.get("email_verified", False):
        return {"message": generic_message}

    now = datetime.now(timezone.utc)
    last_sent = normalize_datetime(
        user.get("password_reset_last_sent_at")
    )

    if (
        last_sent
        and (now - last_sent).total_seconds()
        < PASSWORD_RESET_RESEND_SECONDS
    ):
        return {"message": generic_message}

    # Cryptographically secure, random, single-use token.
    raw_token = secrets.token_urlsafe(48)
    hashed_token = reset_token_hash(raw_token)

    frontend_url = os.getenv(
        "FRONTEND_URL",
        "https://www.rentkhojrkl.in",
    ).rstrip("/")

    reset_url = f"{frontend_url}/reset-password?token={raw_token}"

    try:
        send_password_reset_email(email, reset_url)
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="Unable to send the reset email right now. Please try again later.",
        )

    await users_collection.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "password_reset_token_hash": hashed_token,
                "password_reset_expires_at": now + timedelta(
                    minutes=PASSWORD_RESET_EXPIRY_MINUTES
                ),
                "password_reset_last_sent_at": now,
            }
        },
    )

    return {"message": generic_message}


# =========================
# RESET PASSWORD
# =========================

@router.post("/reset-password")
async def reset_password(data: ResetPasswordRequest):
    hashed_token = reset_token_hash(data.token)
    now = datetime.now(timezone.utc)

    user = await users_collection.find_one(
        {
            "password_reset_token_hash": hashed_token,
            "password_reset_expires_at": {"$gt": now},
        }
    )

    if not user:
        raise HTTPException(
            status_code=400,
            detail="This reset link is invalid or expired. Request a new one.",
        )

    new_password_hash = hash_password(data.new_password)

    # Consume the token atomically: it cannot successfully reset
    # the password more than once.
    result = await users_collection.update_one(
        {
            "_id": user["_id"],
            "password_reset_token_hash": hashed_token,
            "password_reset_expires_at": {"$gt": now},
        },
        {
            "$set": {
                "password_hash": new_password_hash,
                "password_changed_at": now,
            },
            "$unset": {
                "password_reset_token_hash": "",
                "password_reset_expires_at": "",
                "password_reset_last_sent_at": "",
            },
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=400,
            detail="This reset link is invalid, expired, or already used.",
        )

    return {
        "message": (
            "Password reset successfully. "
            "Please log in with your new password."
        )
    }


# =========================
# CURRENT USER
# =========================

@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    return public_user(user)


# =========================
# UPDATE PROFILE
# =========================

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

    phone = (
        (data.phone or "")
        .strip()
        .replace(" ", "")
        .replace("-", "")
        or None
    )

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


# =========================
# UPDATE PHONE
# =========================

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
