from datetime import datetime, timezone
from typing import Literal

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.database import properties_collection, db
from app.permissions import require_permission

router = APIRouter(prefix="/api/admin", tags=["Admin"])


class PropertyReviewRequest(BaseModel):
    status: Literal["approved", "rejected"]


def serialize_admin_property(item: dict) -> dict:
    item = dict(item)
    item["id"] = str(item.pop("_id"))

    for key, value in item.items():
        if isinstance(value, ObjectId):
            item[key] = str(value)

    return item


async def log_admin_activity(
    actor: dict,
    action: str,
    target_type: str,
    target_id: str | None = None,
    details: dict | None = None,
):
    await db["admin_activity"].insert_one({
        "actor_id": actor["_id"],
        "actor_name": actor.get("name"),
        "actor_email": actor.get("email"),
        "actor_role": actor.get("role"),
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "details": details or {},
        "created_at": datetime.now(timezone.utc),
    })


@router.get("/properties")
async def get_properties_for_review(
    review_status: str = "pending",
    admin: dict = Depends(
        require_permission("properties.view")
    ),
):
    allowed_statuses = {"pending", "approved", "rejected", "active"}

    if review_status not in allowed_statuses:
        raise HTTPException(400, "Invalid status filter")

    cursor = properties_collection.find(
        {"status": review_status}
    ).sort("created_at", -1).limit(200)

    results = [
        serialize_admin_property(item)
        async for item in cursor
    ]

    return {
        "count": len(results),
        "properties": results,
    }


@router.patch("/properties/{property_id}/status")
async def review_property(
    property_id: str,
    data: PropertyReviewRequest,
    admin: dict = Depends(
        require_permission("properties.review")
    ),
):
    if not ObjectId.is_valid(property_id):
        raise HTTPException(400, "Invalid property ID")

    object_id = ObjectId(property_id)

    existing = await properties_collection.find_one({
        "_id": object_id
    })

    if not existing:
        raise HTTPException(404, "Property not found")

    if str(existing.get("owner_id")) == str(admin["_id"]):
        raise HTTPException(
            403,
            "You cannot review your own property",
        )

    if existing.get("status") != "pending":
        raise HTTPException(
            409,
            "Only pending properties can be reviewed",
        )

    now = datetime.now(timezone.utc)

    result = await properties_collection.update_one(
        {"_id": object_id, "status": "pending"},
        {
            "$set": {
                "status": data.status,
                "reviewed_by": admin["_id"],
                "reviewed_at": now,
                "updated_at": now,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            409,
            "This property has already been reviewed",
        )

    await log_admin_activity(
        admin,
        f"property.{data.status}",
        "property",
        property_id,
        {"title": existing.get("title")},
    )

    updated = await properties_collection.find_one({
        "_id": object_id
    })

    return {
        "message": f"Property {data.status} successfully",
        "property": serialize_admin_property(updated),
    }
