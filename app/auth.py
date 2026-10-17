
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pymongo.errors import DuplicateKeyError

from app.database import users_collection
from app.schemas import RegisterRequest, LoginRequest, PhoneUpdateRequest
from app.security import (
    hash_password,
    verify_password,
    create_access_token,
    decode_access_token,
)

router = APIRouter(prefix="/api/auth", tags=["Authentication"])
bearer = HTTPBearer(auto_error=False)


class ProfileUpdateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    phone: str | None = Field(default=None, max_length=16)


def public_user(user: dict) -> dict:
    return {
        "id": str(user["_id"]),
        "name": user["name"],
        "email": user["email"],
        "phone": user.get("phone"),
        "role": user["role"],
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

    return user


@router.post("/register", status_code=201)
async def register(data: RegisterRequest):
    email = str(data.email).lower().strip()

    existing = await users_collection.find_one({"email": email})
    if existing:
        raise HTTPException(
            status_code=409,
            detail="An account with this email already exists",
        )

    user = {
        "name": data.name.strip(),
        "email": email,
        "phone": data.phone,
        "password_hash": hash_password(data.password),
        "role": data.role,
        "created_at": datetime.now(timezone.utc),
    }

    try:
        result = await users_collection.insert_one(user)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="An account with this email already exists",
        )

    user["_id"] = result.inserted_id

    token = create_access_token(str(user["_id"]), user["role"])

    return {
        "message": "Account created successfully",
        "access_token": token,
        "token_type": "bearer",
        "user": public_user(user),
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
    """Update a user's display name and contact phone; role/email stay immutable."""
    name = data.name.strip()
    if len(name) < 2:
        raise HTTPException(status_code=422, detail="Name must contain at least 2 characters")

    phone = (data.phone or "").strip().replace(" ", "").replace("-", "") or None
    if phone and not (
        (len(phone) == 10 and phone.isdigit() and phone[0] in "6789")
        or (len(phone) == 13 and phone.startswith("+91") and phone[3] in "6789" and phone[4:].isdigit())
    ):
        raise HTTPException(status_code=422, detail="Enter a valid Indian mobile number or leave it blank")

    await users_collection.update_one(
        {"_id": user["_id"]},
        {"$set": {"name": name, "phone": phone}},
    )
    user["name"] = name
    user["phone"] = phone
    return {"message": "Profile updated successfully", "user": public_user(user)}


@router.patch("/phone")
async def update_phone(
    data: PhoneUpdateRequest,
    user: dict = Depends(get_current_user),
):
    """Let existing accounts add or update their contact number."""
    await users_collection.update_one(
        {"_id": user["_id"]},
        {"$set": {"phone": data.phone}},
    )
    user["phone"] = data.phone
    return {"message": "Phone number updated successfully", "user": public_user(user)}
