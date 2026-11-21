
from datetime import datetime, timezone
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.database import properties_collection, rentals_collection, db

router = APIRouter(prefix="/api/rentals", tags=["Rentals"])


class MarkRentedRequest(BaseModel):
    monthly_rent: int = Field(gt=0, le=10000000)
    start_date: datetime
    end_date: datetime | None = None
    tenant_name: str = Field(min_length=2, max_length=100)
    notes: str | None = Field(default=None, max_length=1000)


class MarkVacantRequest(BaseModel):
    notes: str | None = Field(default=None, max_length=1000)


@router.post("/properties/{property_id}/mark-rented")
async def mark_property_rented(
    property_id: str,
    data: MarkRentedRequest,
    owner: dict = Depends(get_current_user),
):
    if owner.get("role") != "owner":
        raise HTTPException(403, "Only owners can mark properties rented")

    if not ObjectId.is_valid(property_id):
        raise HTTPException(404, "Property not found")

    now = datetime.now(timezone.utc)
    property_doc = await properties_collection.find_one({
        "_id": ObjectId(property_id),
        "owner_id": owner["_id"],
        "deleted_at": None,
    })

    if not property_doc:
        raise HTTPException(404, "Property not found")

    if property_doc.get("rental_status", "available") == "rented":
        raise HTTPException(409, "Property is already marked rented")

    rental = {
        "property_id": property_doc["_id"],
        "owner_id": owner["_id"],
        "tenant_name": data.tenant_name,
        "monthly_rent": data.monthly_rent,
        "start_date": data.start_date,
        "end_date": data.end_date,
        "notes": data.notes,
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "confirmed_by": owner["_id"],
    }

    result = await rentals_collection.insert_one(rental)

    updated = await properties_collection.update_one(
        {
            "_id": property_doc["_id"],
            "owner_id": owner["_id"],
            "rental_status": {"$ne": "rented"},
        },
        {
            "$set": {
                "rental_status": "rented",
                "rental_updated_at": now,
                "updated_at": now,
            }
        },
    )

    if updated.modified_count != 1:
        await rentals_collection.delete_one({"_id": result.inserted_id})
        raise HTTPException(409, "Property status changed. Please retry.")

    await db["admin_activity"].insert_one({
        "actor_id": owner["_id"],
        "actor_name": owner.get("name"),
        "actor_role": "owner",
        "action": "property.marked_rented",
        "target_type": "property",
        "target_id": property_id,
        "created_at": now,
    })

    return {
        "message": "Property marked as rented",
        "rental_id": str(result.inserted_id),
    }


@router.post("/properties/{property_id}/mark-vacant")
async def mark_property_vacant(
    property_id: str,
    data: MarkVacantRequest,
    owner: dict = Depends(get_current_user),
):
    """Close an owner's active rental and make the listing available again."""
    if owner.get("role") != "owner":
        raise HTTPException(403, "Only owners can mark properties vacant")
    if not ObjectId.is_valid(property_id):
        raise HTTPException(404, "Property not found")

    now = datetime.now(timezone.utc)
    property_doc = await properties_collection.find_one({
        "_id": ObjectId(property_id), "owner_id": owner["_id"], "deleted_at": None,
    })
    if not property_doc:
        raise HTTPException(404, "Property not found")
    if property_doc.get("rental_status") != "rented":
        raise HTTPException(409, "Property is not marked rented")

    closed = await rentals_collection.update_one(
        {"property_id": property_doc["_id"], "owner_id": owner["_id"], "status": "active"},
        {"$set": {"status": "completed", "end_date": now, "completion_notes": data.notes, "updated_at": now}},
    )
    if closed.modified_count != 1:
        raise HTTPException(409, "Active rental record not found")

    await properties_collection.update_one(
        {"_id": property_doc["_id"], "owner_id": owner["_id"]},
        {"$set": {"rental_status": "vacant", "rental_updated_at": now, "updated_at": now}},
    )
    await db["admin_activity"].insert_one({
        "actor_id": owner["_id"], "actor_name": owner.get("name"), "actor_role": "owner",
        "action": "property.marked_vacant", "target_type": "property", "target_id": property_id,
        "created_at": now,
    })
    return {"message": "Property marked vacant"}


@router.get("/mine")
async def my_rentals(user: dict = Depends(get_current_user)):
    if user.get("role") != "owner":
        raise HTTPException(403, "Only owners can view their rentals")

    cursor = rentals_collection.find({
        "owner_id": user["_id"]
    }).sort("created_at", -1).limit(100)

    rentals = []

    async for item in cursor:
        item["id"] = str(item.pop("_id"))
        item["property_id"] = str(item["property_id"])
        item["owner_id"] = str(item["owner_id"])
        item["confirmed_by"] = str(item["confirmed_by"])
        rentals.append(item)

    return {"rentals": rentals}
