from datetime import datetime, timezone
from typing import Literal

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.auth import get_current_user
from app.database import requirements_collection, properties_collection

router = APIRouter(prefix="/api/requirements", tags=["Room Requirements"])

ROOM_TYPES = {"Single Room", "Shared Room", "Room", "Flat", "PG", "1 BHK"}
FURNISHING = {"Any", "Furnished", "Semi-furnished", "Unfurnished"}
MOVE_IN = {"Immediately", "Within 15 days", "Within a month", "Flexible"}


class RequirementCreate(BaseModel):
    room_type: str = Field(min_length=2, max_length=40)
    area: str = Field(min_length=2, max_length=100)
    monthly_budget: int = Field(gt=0, le=1000000)
    move_in: str = Field(min_length=2, max_length=40)
    furnishing: str = "Any"
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("room_type")
    @classmethod
    def valid_room_type(cls, value):
        if value not in ROOM_TYPES:
            raise ValueError("Unsupported room type")
        return value

    @field_validator("furnishing")
    @classmethod
    def valid_furnishing(cls, value):
        if value not in FURNISHING:
            raise ValueError("Unsupported furnishing preference")
        return value

    @field_validator("move_in")
    @classmethod
    def valid_move_in(cls, value):
        if value not in MOVE_IN:
            raise ValueError("Unsupported move-in preference")
        return value


class RequirementUpdate(BaseModel):
    status: Literal["open", "closed"]


def serialize(item: dict) -> dict:
    item = dict(item)
    item["id"] = str(item.pop("_id"))
    for key, value in item.items():
        if isinstance(value, ObjectId):
            item[key] = str(value)
    return item


def tenant_only(user: dict):
    if user.get("role") != "tenant":
        raise HTTPException(status_code=403, detail="Only tenant accounts can manage room requirements")
    return user


def owner_only(user: dict):
    if user.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Only property owners can view matching requirements")
    return user


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_requirement(data: RequirementCreate, user: dict = Depends(get_current_user)):
    tenant_only(user)
    now = datetime.now(timezone.utc)
    item = data.model_dump()
    item.update({
        "tenant_id": user["_id"],
        "tenant_name": user.get("name", "RentKhoj tenant"),
        "status": "open",
        "created_at": now,
        "updated_at": now,
    })
    result = await requirements_collection.insert_one(item)
    item["_id"] = result.inserted_id
    return serialize(item)


@router.get("/mine")
async def my_requirements(user: dict = Depends(get_current_user)):
    tenant_only(user)
    cursor = requirements_collection.find({"tenant_id": user["_id"]}).sort("created_at", -1).limit(100)
    return {"requirements": [serialize(item) async for item in cursor]}


@router.get("/owner/matching")
async def owner_matching_requirements(user: dict = Depends(get_current_user)):
    owner_only(user)
    # Only open requirements are visible to authenticated property owners.
    cursor = requirements_collection.find({"status": "open"}).sort("created_at", -1).limit(100)
    results = []
    async for req in cursor:
        # Keep this endpoint relevant to properties the owner actually has listed.
        props = properties_collection.find({"owner_id": user["_id"], "status": {"$in": ["active", "approved"]}, "rent": {"$lte": req["monthly_budget"]}})
        has_match = False
        async for prop in props:
            wanted_area = req["area"].lower()
            if wanted_area in str(prop.get("area", "")).lower() or wanted_area in str(prop.get("address", "")).lower():
                wanted_types = {"Single Room": {"Room"}, "Shared Room": {"Room", "PG"}, "Room": {"Room"}, "Flat": {"Flat"}, "1 BHK": {"Flat"}, "PG": {"PG"}}
                if prop.get("property_type") in wanted_types.get(req["room_type"], {req["room_type"]}):
                    has_match = True
                    break
        if has_match:
            results.append(serialize(req))
    return {"count": len(results), "requirements": results}


@router.patch("/{requirement_id}")
async def update_requirement(requirement_id: str, data: RequirementUpdate, user: dict = Depends(get_current_user)):
    tenant_only(user)
    if not ObjectId.is_valid(requirement_id):
        raise HTTPException(status_code=404, detail="Requirement not found")
    result = await requirements_collection.update_one(
        {"_id": ObjectId(requirement_id), "tenant_id": user["_id"]},
        {"$set": {"status": data.status, "updated_at": datetime.now(timezone.utc)}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Requirement not found")
    item = await requirements_collection.find_one({"_id": ObjectId(requirement_id)})
    return serialize(item)


@router.get("/{requirement_id}/matches")
async def matching_properties(requirement_id: str, user: dict = Depends(get_current_user)):
    tenant_only(user)
    if not ObjectId.is_valid(requirement_id):
        raise HTTPException(status_code=404, detail="Requirement not found")
    req = await requirements_collection.find_one({"_id": ObjectId(requirement_id), "tenant_id": user["_id"]})
    if not req:
        raise HTTPException(status_code=404, detail="Requirement not found")

    type_map = {
        "Single Room": ["Room"], "Shared Room": ["Room", "PG"],
        "Room": ["Room"], "Flat": ["Flat"], "1 BHK": ["Flat"], "PG": ["PG"],
    }
    query = {
        "status": {"$in": ["active", "approved"]},
        "rent": {"$lte": req["monthly_budget"]},
        "property_type": {"$in": type_map.get(req["room_type"], [req["room_type"]])},
    }
    if req.get("furnishing") and req["furnishing"] != "Any":
        query["furnishing"] = req["furnishing"]
    cursor = properties_collection.find(query).sort("created_at", -1).limit(100)
    area = req["area"].strip().lower()
    matches = []
    async for item in cursor:
        item_area = str(item.get("area", "")).lower()
        item_address = str(item.get("address", "")).lower()
        if area in item_area or area in item_address or not area:
            matches.append(serialize(item))
    return {"count": len(matches), "properties": matches}


