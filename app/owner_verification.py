
from datetime import datetime, timezone
from typing import Literal

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.database import db, users_collection, properties_collection
from app.permissions import require_permission

router = APIRouter(tags=["Owner Verification"])

requests_collection = db["owner_verification_requests"]


class VerificationRequestCreate(BaseModel):
    legal_name: str = Field(min_length=2, max_length=100)
    proof_method: Literal[
        "in_person",
        "ownership_document",
        "authorization_letter",
    ]
    note: str = Field(default="", max_length=1000)
    declaration: bool


class VerificationDecision(BaseModel):
    status: Literal["verified", "rejected"]
    decision_note: str = Field(default="", max_length=1000)


def serialize_request(item: dict) -> dict:
    item = dict(item)
    item["id"] = str(item.pop("_id"))

    for key in ("owner_id", "reviewed_by"):
        if isinstance(item.get(key), ObjectId):
            item[key] = str(item[key])

    return item


async def log_verification_activity(
    actor: dict,
    action: str,
    request_id: str,
    details: dict,
):
    await db["admin_activity"].insert_one({
        "actor_id": actor["_id"],
        "actor_name": actor.get("name"),
        "actor_email": actor.get("email"),
        "actor_role": actor.get("role"),
        "action": action,
        "target_type": "owner_verification",
        "target_id": request_id,
        "details": details,
        "created_at": datetime.now(timezone.utc),
    })


@router.get("/api/owner-verification/me")
async def my_verification(
    owner: dict = Depends(get_current_user),
):
    if owner.get("role") != "owner":
        raise HTTPException(403, "Only owners can access verification.")

    latest = await requests_collection.find_one(
        {"owner_id": owner["_id"]},
        sort=[("created_at", -1)],
    )

    return {
        "status": owner.get("owner_verification_status", "unverified"),
        "verified_at": owner.get("owner_verified_at"),
        "request": serialize_request(latest) if latest else None,
    }


@router.post("/api/owner-verification/request", status_code=201)
async def submit_verification(
    data: VerificationRequestCreate,
    owner: dict = Depends(get_current_user),
):
    if owner.get("role") != "owner":
        raise HTTPException(403, "Only property owners can apply.")

    if not data.declaration:
        raise HTTPException(
            422,
            "You must confirm that the information is accurate.",
        )

    if owner.get("owner_verification_status") == "verified":
        raise HTTPException(409, "Your owner account is already verified.")

    pending = await requests_collection.find_one({
        "owner_id": owner["_id"],
        "status": "pending",
    })

    if pending:
        raise HTTPException(
            409,
            "You already have a verification request under review.",
        )

    now = datetime.now(timezone.utc)

    document = {
        "owner_id": owner["_id"],
        "owner_name": owner.get("name"),
        "owner_email": owner.get("email"),
        "owner_phone": owner.get("phone"),
        "legal_name": data.legal_name.strip(),
        "proof_method": data.proof_method,
        "note": data.note.strip(),
        "status": "pending",
        "created_at": now,
        "reviewed_at": None,
        "reviewed_by": None,
        "decision_note": None,
    }

    result = await requests_collection.insert_one(document)

    await users_collection.update_one(
        {"_id": owner["_id"]},
        {"$set": {"owner_verification_status": "pending"}},
    )

    return {
        "message": "Verification request submitted.",
        "request_id": str(result.inserted_id),
        "status": "pending",
    }


@router.get("/api/admin/owner-verifications")
async def list_verification_requests(
    review_status: Literal["pending", "verified", "rejected"] = "pending",
    limit: int = 100,
    admin: dict = Depends(require_permission("owners.verify")),
):
    limit = max(1, min(limit, 200))

    cursor = requests_collection.find({
        "status": review_status,
    }).sort("created_at", -1).limit(limit)

    return {
        "requests": [
            serialize_request(item)
            async for item in cursor
        ]
    }


@router.patch("/api/admin/owner-verifications/{request_id}")
async def review_verification_request(
    request_id: str,
    data: VerificationDecision,
    admin: dict = Depends(require_permission("owners.verify")),
):
    if not ObjectId.is_valid(request_id):
        raise HTTPException(400, "Invalid verification request ID.")

    request = await requests_collection.find_one({
        "_id": ObjectId(request_id),
        "status": "pending",
    })

    if not request:
        raise HTTPException(
            404,
            "Pending verification request not found.",
        )

    if request["owner_id"] == admin["_id"]:
        raise HTTPException(403, "You cannot review your own request.")

    now = datetime.now(timezone.utc)

    result = await requests_collection.update_one(
        {
            "_id": request["_id"],
            "status": "pending",
        },
        {
            "$set": {
                "status": data.status,
                "reviewed_at": now,
                "reviewed_by": admin["_id"],
                "reviewer_name": admin.get("name"),
                "decision_note": data.decision_note.strip(),
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            409,
            "This verification request has already been reviewed.",
        )

    owner_status = (
        "verified" if data.status == "verified" else "rejected"
    )

    await users_collection.update_one(
        {"_id": request["owner_id"], "role": "owner"},
        {
            "$set": {
                "owner_verification_status": owner_status,
                "owner_verified_at": now if owner_status == "verified" else None,
                "owner_verified_by": admin["_id"] if owner_status == "verified" else None,
            }
        },
    )

    # Keep existing listings in sync with the owner's verification status.
    await properties_collection.update_many(
        {"owner_id": request["owner_id"]},
        {"$set": {"owner_verified": owner_status == "verified"}},
    )

    await log_verification_activity(
        admin,
        f"owner_verification.{data.status}",
        request_id,
        {
            "owner_id": str(request["owner_id"]),
            "owner_email": request.get("owner_email"),
            "decision_note": data.decision_note.strip(),
        },
    )

    return {
        "message": f"Owner verification {data.status}.",
        "status": owner_status,
    }
