
from datetime import datetime, timezone
from typing import Literal

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth import get_current_user
from app.database import properties_collection

router = APIRouter(prefix="/api/admin", tags=["Admin"])


class PropertyReviewRequest(BaseModel):
    status: Literal["approved", "rejected"]


def require_admin(user: dict = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user



def serialize_admin_property(item: dict) -> dict:
    item = dict(item)
    item["id"] = str(item.pop("_id"))

    # Convert MongoDB ObjectId fields to JSON-safe strings.
    for key, value in item.items():
        if isinstance(value, ObjectId):
            item[key] = str(value)

    return item



@router.get("/properties")
async def get_properties_for_review(
    review_status: str = "pending",
    admin: dict = Depends(require_admin),
):
    allowed_statuses = {"pending", "approved", "rejected"}

    if review_status not in allowed_statuses:
        raise HTTPException(
            status_code=400,
            detail="Invalid status filter",
        )

    cursor = properties_collection.find(
        {"status": review_status}
    ).sort("created_at", -1)

    results = []
    async for item in cursor:
        results.append(serialize_admin_property(item))

    return {"count": len(results), "properties": results}


@router.patch("/properties/{property_id}/status")
async def review_property(
    property_id: str,
    data: PropertyReviewRequest,
    admin: dict = Depends(require_admin),
):
    if not ObjectId.is_valid(property_id):
        raise HTTPException(
            status_code=400,
            detail="Invalid property ID",
        )

    object_id = ObjectId(property_id)
    existing = await properties_collection.find_one(
        {"_id": object_id}
    )

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Property not found",
        )

    # An admin must not approve or reject their own listing.
    if str(existing.get("owner_id")) == str(admin["_id"]):
        raise HTTPException(
            status_code=403,
            detail="You cannot review your own property",
        )

    if existing.get("status") != "pending":
        raise HTTPException(
            status_code=409,
            detail="Only pending properties can be reviewed",
        )

    await properties_collection.update_one(
        {"_id": object_id, "status": "pending"},
        {
            "$set": {
                "status": data.status,
                "reviewed_by": admin["_id"],
                "reviewed_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )

    updated = await properties_collection.find_one(
        {"_id": object_id}
    )

    return {
        "message": f"Property {data.status} successfully",
        "property": serialize_admin_property(updated),
    }
