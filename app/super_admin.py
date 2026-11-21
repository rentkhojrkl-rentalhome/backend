
from datetime import datetime, timezone
from typing import Literal

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
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
from app.security import hash_password, verify_password
from app.database import rentals_collection, backup_metadata_collection
from app.export_service import build_export

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


class DeletionRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class PermanentDeletionRequest(DeletionRequest):
    target_id_confirmation: str = Field(min_length=1, max_length=64)
    current_password: str = Field(min_length=8, max_length=128)


class RentalStatusRequest(BaseModel):
    rental_status: Literal["available", "rented", "vacant"]
    reason: str = Field(min_length=3, max_length=500)


class PropertyStatusRequest(BaseModel):
    status: Literal["pending", "approved", "rejected", "active"]
    reason: str = Field(min_length=3, max_length=500)


class BackupMetadataRequest(BaseModel):
    backup_id: str = Field(min_length=8, max_length=160)
    storage_key: str = Field(min_length=3, max_length=500)
    size_bytes: int = Field(ge=0)
    verified: bool = False


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
        "deleted_at": user.get("deleted_at"),
    }


def serialize_document(document: dict) -> dict:
    result = dict(document)
    result["id"] = str(result.pop("_id"))
    for key, value in list(result.items()):
        if isinstance(value, ObjectId):
            result[key] = str(value)
    return result


def pagination(page: int, page_size: int) -> tuple[int, int]:
    return max(1, page), max(1, min(page_size, 100))


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
    active_filter = {"deleted_at": None}
    total_users = await users_collection.count_documents({"role": {"$in": ["tenant", "owner"]}, **active_filter})

    total_admins = await users_collection.count_documents({"role": "admin", **active_filter})

    total_properties = await properties_collection.count_documents({"deleted_at": None})
    pending = await properties_collection.count_documents({
        "status": "pending", "deleted_at": None
    })
    approved = await properties_collection.count_documents({
        "status": {"$in": ["approved", "active"]}, "deleted_at": None
    })
    rejected = await properties_collection.count_documents({
        "status": "rejected", "deleted_at": None
    })

    tenants = await users_collection.count_documents({
        "role": "tenant", **active_filter
    })
    owners = await users_collection.count_documents({
        "role": "owner", **active_filter
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
            "rented": await properties_collection.count_documents({"rental_status": "rented", "deleted_at": None}),
        },
        "rentals": {"active": await rentals_collection.count_documents({"status": "active"})},
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
    search: str | None = None,
    page: int = 1,
    page_size: int = 25,
    include_deleted: bool = False,
    user: dict = Depends(require_super_admin),
):
    query =  {"role": {"$in": ["tenant", "owner"]}}
    if role:
        query["role"] = role
    if not include_deleted:
        query["deleted_at"] = None
    if search:
        query["$or"] = [{"name": {"$regex": search, "$options": "i"}}, {"email": {"$regex": search, "$options": "i"}}, {"phone": {"$regex": search, "$options": "i"}}]
    page, page_size = pagination(page, page_size)
    total = await users_collection.count_documents(query)

    cursor = users_collection.find(
        query,
        {
            "password_hash": 0,
            "email_otp_hash": 0,
            "password_reset_token_hash": 0,
        },
    ).sort("created_at", -1).skip((page - 1) * page_size).limit(page_size)

    return {
        "users": [
            serialize_user(item)
            async for item in cursor
        ], "total": total, "page": page, "page_size": page_size
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


@router.get("/properties")
async def list_properties(
    search: str | None = None, status: str | None = None, rental_status: str | None = None,
    page: int = 1, page_size: int = 25, include_deleted: bool = False,
    user: dict = Depends(require_super_admin),
):
    query: dict = {}
    if not include_deleted:
        query["deleted_at"] = None
    if status:
        query["status"] = status
    if rental_status:
        query["rental_status"] = rental_status
    if search:
        query["$or"] = [{"title": {"$regex": search, "$options": "i"}}, {"area": {"$regex": search, "$options": "i"}}]
    page, page_size = pagination(page, page_size)
    total = await properties_collection.count_documents(query)
    cursor = properties_collection.find(query).sort("created_at", -1).skip((page - 1) * page_size).limit(page_size)
    return {"properties": [serialize_document(item) async for item in cursor], "total": total, "page": page, "page_size": page_size}


@router.get("/rentals")
async def list_rentals(
    status: Literal["active", "completed", "cancelled"] | None = None,
    page: int = 1, page_size: int = 25, user: dict = Depends(require_super_admin),
):
    query = {"status": status} if status else {}
    page, page_size = pagination(page, page_size)
    total = await rentals_collection.count_documents(query)
    cursor = rentals_collection.find(query).sort("created_at", -1).skip((page - 1) * page_size).limit(page_size)
    return {"rentals": [serialize_document(item) async for item in cursor], "total": total, "page": page, "page_size": page_size}


@router.patch("/users/{user_id}/delete")
async def soft_delete_user(user_id: str, data: DeletionRequest, super_admin: dict = Depends(require_super_admin)):
    if not ObjectId.is_valid(user_id): raise HTTPException(400, "Invalid user ID")
    target = await users_collection.find_one({"_id": ObjectId(user_id), "role": {"$in": ["tenant", "owner"]}, "deleted_at": None})
    if not target: raise HTTPException(404, "User not found")
    now = datetime.now(timezone.utc)
    await users_collection.update_one({"_id": target["_id"]}, {"$set": {"deleted_at": now, "deleted_by": super_admin["_id"], "deletion_reason": data.reason, "is_active": False}})
    await log_super_activity(super_admin, "user.soft_deleted", user_id, {"reason": data.reason})
    return {"message": "User soft deleted"}


@router.patch("/users/{user_id}/restore")
async def restore_user(user_id: str, super_admin: dict = Depends(require_super_admin)):
    if not ObjectId.is_valid(user_id): raise HTTPException(400, "Invalid user ID")
    result = await users_collection.update_one({"_id": ObjectId(user_id), "role": {"$in": ["tenant", "owner"]}, "deleted_at": {"$exists": True}}, {"$set": {"is_active": True}, "$unset": {"deleted_at": "", "deleted_by": "", "deletion_reason": ""}})
    if result.modified_count != 1: raise HTTPException(404, "Deleted user not found")
    await log_super_activity(super_admin, "user.restored", user_id)
    return {"message": "User restored"}


@router.patch("/properties/{property_id}/delete")
async def soft_delete_property(property_id: str, data: DeletionRequest, super_admin: dict = Depends(require_super_admin)):
    if not ObjectId.is_valid(property_id): raise HTTPException(400, "Invalid property ID")
    now = datetime.now(timezone.utc)
    result = await properties_collection.update_one({"_id": ObjectId(property_id), "deleted_at": None}, {"$set": {"deleted_at": now, "deleted_by": super_admin["_id"], "deletion_reason": data.reason}})
    if result.modified_count != 1: raise HTTPException(404, "Property not found")
    await log_super_activity(super_admin, "property.soft_deleted", property_id, {"reason": data.reason})
    return {"message": "Property soft deleted"}


@router.patch("/properties/{property_id}/restore")
async def restore_property(property_id: str, super_admin: dict = Depends(require_super_admin)):
    if not ObjectId.is_valid(property_id): raise HTTPException(400, "Invalid property ID")
    result = await properties_collection.update_one({"_id": ObjectId(property_id), "deleted_at": {"$exists": True}}, {"$unset": {"deleted_at": "", "deleted_by": "", "deletion_reason": ""}})
    if result.modified_count != 1: raise HTTPException(404, "Deleted property not found")
    await log_super_activity(super_admin, "property.restored", property_id)
    return {"message": "Property restored"}


@router.patch("/properties/{property_id}/rental-status")
async def override_rental_status(property_id: str, data: RentalStatusRequest, super_admin: dict = Depends(require_super_admin)):
    if not ObjectId.is_valid(property_id): raise HTTPException(400, "Invalid property ID")
    result = await properties_collection.update_one({"_id": ObjectId(property_id), "deleted_at": None}, {"$set": {"rental_status": data.rental_status, "rental_updated_at": datetime.now(timezone.utc)}})
    if result.modified_count != 1: raise HTTPException(404, "Property not found")
    await log_super_activity(super_admin, "property.rental_status_overridden", property_id, {"rental_status": data.rental_status, "reason": data.reason})
    return {"message": "Rental status updated"}


@router.patch("/properties/{property_id}/status")
async def override_property_status(property_id: str, data: PropertyStatusRequest, super_admin: dict = Depends(require_super_admin)):
    if not ObjectId.is_valid(property_id): raise HTTPException(400, "Invalid property ID")
    now = datetime.now(timezone.utc)
    result = await properties_collection.update_one(
        {"_id": ObjectId(property_id), "deleted_at": None},
        {"$set": {"status": data.status, "reviewed_by": super_admin["_id"], "reviewed_at": now, "updated_at": now}},
    )
    if result.modified_count != 1: raise HTTPException(404, "Property not found")
    await log_super_activity(super_admin, "property.status_overridden", property_id, {"status": data.status, "reason": data.reason})
    return {"message": "Property status updated"}


@router.patch("/owners/{owner_id}/verification/revoke")
async def revoke_owner_verification(owner_id: str, data: DeletionRequest, super_admin: dict = Depends(require_super_admin)):
    if not ObjectId.is_valid(owner_id): raise HTTPException(400, "Invalid owner ID")
    owner = await users_collection.find_one({"_id": ObjectId(owner_id), "role": "owner"})
    if not owner: raise HTTPException(404, "Owner not found")
    now = datetime.now(timezone.utc)
    await users_collection.update_one({"_id": owner["_id"]}, {"$set": {"owner_verification_status": "unverified", "owner_verification_revoked_at": now, "owner_verification_revoked_by": super_admin["_id"]}, "$unset": {"owner_verified_at": "", "owner_verified_by": ""}})
    await properties_collection.update_many({"owner_id": owner["_id"]}, {"$set": {"owner_verified": False}})
    await log_super_activity(super_admin, "owner_verification.revoked", owner_id, {"reason": data.reason})
    return {"message": "Owner verification revoked"}


@router.delete("/users/{user_id}/permanent")
async def permanently_delete_user(user_id: str, data: PermanentDeletionRequest, super_admin: dict = Depends(require_super_admin)):
    if data.target_id_confirmation != user_id: raise HTTPException(422, "Target ID confirmation does not match")
    if not verify_password(data.current_password, super_admin.get("password_hash", "")): raise HTTPException(403, "Recent password confirmation failed")
    if not ObjectId.is_valid(user_id): raise HTTPException(400, "Invalid user ID")
    target = await users_collection.find_one({"_id": ObjectId(user_id), "role": {"$in": ["tenant", "owner"]}, "deleted_at": {"$exists": True}})
    if not target: raise HTTPException(404, "Soft-deleted user not found")
    linked = await rentals_collection.count_documents({"owner_id": target["_id"]})
    if linked: raise HTTPException(409, "Cannot permanently delete a user with rental history; retain or anonymize it first")
    await users_collection.delete_one({"_id": target["_id"]})
    await log_super_activity(super_admin, "user.permanently_deleted", user_id, {"reason": data.reason})
    return {"message": "User permanently deleted"}


@router.delete("/properties/{property_id}/permanent")
async def permanently_delete_property(property_id: str, data: PermanentDeletionRequest, super_admin: dict = Depends(require_super_admin)):
    if data.target_id_confirmation != property_id: raise HTTPException(422, "Target ID confirmation does not match")
    if not verify_password(data.current_password, super_admin.get("password_hash", "")): raise HTTPException(403, "Recent password confirmation failed")
    if not ObjectId.is_valid(property_id): raise HTTPException(400, "Invalid property ID")
    linked = await rentals_collection.count_documents({"property_id": ObjectId(property_id)})
    if linked: raise HTTPException(409, "Cannot permanently delete property with rental history")
    result = await properties_collection.delete_one({"_id": ObjectId(property_id), "deleted_at": {"$exists": True}})
    if result.deleted_count != 1: raise HTTPException(404, "Soft-deleted property not found")
    await log_super_activity(super_admin, "property.permanently_deleted", property_id, {"reason": data.reason})
    return {"message": "Property permanently deleted"}


@router.get("/export/{collection}")
async def export_collection(collection: str, format: Literal["csv", "xlsx"] = "csv", user: dict = Depends(require_super_admin)):
    response = await build_export(collection, format)
    await log_super_activity(user, "database.exported", collection, {"format": format})
    return response


@router.get("/backups")
async def list_backups(user: dict = Depends(require_super_admin)):
    cursor = backup_metadata_collection.find({}).sort("created_at", -1).limit(100)
    return {"backups": [serialize_document(item) async for item in cursor]}


@router.post("/backups/metadata", status_code=201)
async def record_backup_metadata(data: BackupMetadataRequest, user: dict = Depends(require_super_admin)):
    now = datetime.now(timezone.utc)
    result = await backup_metadata_collection.insert_one({**data.model_dump(), "type": "full", "status": "completed", "created_at": now, "created_by": user["_id"]})
    await log_super_activity(user, "database.backup_recorded", str(result.inserted_id), {"backup_id": data.backup_id})
    return {"message": "Backup metadata recorded", "id": str(result.inserted_id)}


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
