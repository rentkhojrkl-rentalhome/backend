
from datetime import datetime, timezone
from typing import Literal

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from app.database import (
    db,
    users_collection,
    properties_collection,
)
from app.permissions import (
    require_super_admin,
    ALL_PERMISSIONS,
)
from app.security import hash_password

router = APIRouter(
    prefix="/api/super-admin",
    tags=["Super Admin"],
)


class CreateAdminRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    permissions: list[str] =  Field(default_factory=list)


class PermissionUpdateRequest(BaseModel):
    permissions: list[str]


class ActiveUpdateRequest(BaseModel):
    is_active: bool


def serialize_user(user: dict) -> dict:
    return {
        "id": str(user["_id"]),
        "name": user.get("name"),
        "email": user.get("email"),
        "phone": user.get("phone"),
        "role": user.get("role"),
        "is_active": user.get("is_active", True),
        "email_verified": user.get("email_verified", False),
        "permissions": user.get("permissions", []),
        "created_at": user.get("created_at"),
    }


async def log_super_activity(
    actor: dict,
    action: str,
    target_id: str | None = None,
    details: dict | None = None,
):
    await db["admin_activity"].insert_one({
        "actor_id": actor["_id"],
        "actor_name": actor.get("name"),
        "actor_email": actor.get("email"),
        "actor_role": "super_admin",
        "action": action,
        "target_type": "admin_or_platform",
        "target_id": target_id,
        "details": details or {},
        "created_at": datetime.now(timezone.utc),
    })


@router.get("/overview")
async def overview(
    user: dict = Depends(require_super_admin),
):
    total_users = await users_collection.count_documents({
        "role": {"$in": ["tenant", "owner"]}
    })

    total_admins = await users_collection.count_documents({
        "role": "admin"
    })

    total_properties = await properties_collection.count_documents({})
    pending = await properties_collection.count_documents({
        "status": "pending"
    })
    approved = await properties_collection.count_documents({
        "status": {"$in": ["approved", "active"]}
    })
    rejected = await properties_collection.count_documents({
        "status": "rejected"
    })

    tenants = await users_collection.count_documents({
        "role": "tenant"
    })
    owners = await users_collection.count_documents({
        "role": "owner"
    })

    return {
        "users": {
            "total": total_users,
            "tenants": tenants,
            "owners": owners,
            "admins": total_admins,
        },
        "properties": {
            "total": total_properties,
            "pending": pending,
            "approved": approved,
            "rejected": rejected,
        },
        "revenue": {
            "available": False,
            "total": None,
            "currency": "INR",
            "message": "Payment integration is not configured.",
        },
    }


@router.get("/users")
async def list_users(
    role: Literal["tenant", "owner", "admin"] | None = None,
    limit: int = 100,
    user: dict = Depends(require_super_admin),
):
    limit = max(1, min(limit, 200))

    query =  {"role": {"$in": ["tenant", "owner"]}}
    if role:
        query["role"] = role

    cursor = users_collection.find(
        query,
        {
            "password_hash": 0,
            "email_otp_hash": 0,
            "password_reset_token_hash": 0,
        },
    ).sort("created_at", -1).limit(limit)

    return {
        "users": [
            serialize_user(item)
            async for item in cursor
        ]
    }


@router.get("/admins")
async def list_admins(
    user: dict = Depends(require_super_admin),
):
    cursor = users_collection.find({
        "role": "admin"
    }).sort("created_at", -1)

    return {
        "admins": [
            serialize_user(item)
            async for item in cursor
        ]
    }


@router.post("/admins", status_code=201)
async def create_admin(
    data: CreateAdminRequest,
    super_admin: dict = Depends(require_super_admin),
):
    email = str(data.email).lower().strip()

    invalid = set(data.permissions) - ALL_PERMISSIONS
    if invalid:
        raise HTTPException(
            422,
            f"Invalid permissions: {sorted(invalid)}",
        )

    existing = await users_collection.find_one({
        "email": email
    })

    if existing:
        raise HTTPException(
            409,
            "An account with this email already exists",
        )

    now = datetime.now(timezone.utc)

    admin_data = {
        "name": data.name.strip(),
        "email": email,
        "password_hash": hash_password(data.password),
        "phone": None,
        "role": "admin",
        "permissions": sorted(set(data.permissions)),
        "email_verified": True,
        "is_active": True,
        "created_at": now,
        "created_by": super_admin["_id"],
    }

    result = await users_collection.insert_one(admin_data)

    await log_super_activity(
        super_admin,
        "admin.created",
        str(result.inserted_id),
        {"email": email, "permissions": admin_data["permissions"]},
    )

    saved = await users_collection.find_one({
        "_id": result.inserted_id
    })

    return {
        "message": "Admin created successfully",
        "admin": serialize_user(saved),
    }


@router.patch("/admins/{admin_id}/permissions")
async def update_admin_permissions(
    admin_id: str,
    data: PermissionUpdateRequest,
    super_admin: dict = Depends(require_super_admin),
):
    if not ObjectId.is_valid(admin_id):
        raise HTTPException(400, "Invalid admin ID")

    invalid = set(data.permissions) - ALL_PERMISSIONS
    if invalid:
        raise HTTPException(
            422,
            f"Invalid permissions: {sorted(invalid)}",
        )

    target = await users_collection.find_one({
        "_id": ObjectId(admin_id),
        "role": "admin",
    })

    if not target:
        raise HTTPException(404, "Admin not found")

    permissions = sorted(set(data.permissions))

    await users_collection.update_one(
        {"_id": target["_id"], "role": "admin"},
        {"$set": {"permissions": permissions}},
    )

    await log_super_activity(
        super_admin,
        "admin.permissions_updated",
        admin_id,
        {"permissions": permissions},
    )

    return {
        "message": "Admin permissions updated",
        "permissions": permissions,
    }


@router.patch("/admins/{admin_id}/active")
async def update_admin_active(
    admin_id: str,
    data: ActiveUpdateRequest,
    super_admin: dict = Depends(require_super_admin),
):
    if not ObjectId.is_valid(admin_id):
        raise HTTPException(400, "Invalid admin ID")

    target = await users_collection.find_one({
        "_id": ObjectId(admin_id),
        "role": "admin",
    })

    if not target:
        raise HTTPException(404, "Admin not found")

    await users_collection.update_one(
        {"_id": target["_id"]},
        {"$set": {"is_active": data.is_active}},
    )

    await log_super_activity(
        super_admin,
        "admin.active_changed",
        admin_id,
        {"is_active": data.is_active},
    )

    return {"message": "Admin status updated"}


@router.patch("/users/{user_id}/active")
async def update_user_active(
    user_id: str,
    data: ActiveUpdateRequest,
    super_admin: dict = Depends(require_super_admin),
):
    if not ObjectId.is_valid(user_id):
        raise HTTPException(400, "Invalid user ID")

    target = await users_collection.find_one({
        "_id": ObjectId(user_id),
        "role": {"$in": ["tenant", "owner"]},
    })

    if not target:
        raise HTTPException(
            404,
            "Tenant or owner account not found",
        )

    await users_collection.update_one(
        {"_id": target["_id"]},
        {"$set": {"is_active": data.is_active}},
    )

    await log_super_activity(
        super_admin,
        "user.active_changed",
        user_id,
        {"is_active": data.is_active},
    )

    return {"message": "User status updated"}



@router.get("/activity")
async def get_activity(
    limit: int = 100,
    user: dict = Depends(require_super_admin),
):
    limit = max(1, min(limit, 200))

    cursor = db["admin_activity"].find({}).sort(
        "created_at", -1
    ).limit(limit)

    results = []

    async for item in cursor:
        item["id"] = str(item.pop("_id"))
        item["actor_id"] = str(item["actor_id"])
        results.append(item)

    return {"activities": results}


@router.get("/revenue")
async def revenue(
    user: dict = Depends(require_super_admin),
):
    # No payment/transaction collection exists in the inspected codebase.
    return {
        "available": False,
        "currency": "INR",
        "total": None,
        "transactions": 0,
        "message": (
            "Revenue analytics will become available "
            "after payment integration is implemented."
        ),
    }
