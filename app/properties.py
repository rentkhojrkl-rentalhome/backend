from datetime import datetime, timezone
from io import BytesIO
import os

import cloudinary
import cloudinary.uploader
from bson import ObjectId
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pymongo.errors import PyMongoError

from app.auth import get_current_user
from app.database import properties_collection, users_collection
from app.property_schemas import PropertyCreate

router = APIRouter(prefix="/api/properties", tags=["Properties"])

MAX_IMAGES = 10
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB per image
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}


def cloudinary_is_configured() -> bool:
    return all(
        os.getenv(key)
        for key in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET")
    )


def configure_cloudinary() -> None:
    cloudinary.config(
        cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
        api_key=os.getenv("CLOUDINARY_API_KEY"),
        api_secret=os.getenv("CLOUDINARY_API_SECRET"),
        secure=True,
    )


def serialize_property(item: dict) -> dict:
    item = dict(item)
    item["id"] = str(item.pop("_id"))
    for key, value in item.items():
        if isinstance(value, ObjectId):
            item[key] = str(value)
    return item


def require_owner(user: dict = Depends(get_current_user)):
    if user.get("role") != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only property owners can perform this action",
        )
    return user


@router.post("/upload-images")
async def upload_property_images(
    files: list[UploadFile] = File(...),
    owner: dict = Depends(require_owner),
):
    """Upload images to Cloudinary. Only authenticated owner accounts may upload."""
    if not cloudinary_is_configured():
        raise HTTPException(
            status_code=503,
            detail="Image uploads are not configured yet. Add Cloudinary credentials to backend/.env.",
        )

    if not files:
        raise HTTPException(status_code=422, detail="Choose at least one image")
    if len(files) > MAX_IMAGES:
        raise HTTPException(status_code=422, detail=f"You can upload up to {MAX_IMAGES} photos per property")

    # Validate all files before uploading any of them.
    file_contents: list[tuple[UploadFile, bytes]] = []
    for file in files:
        if file.content_type not in ALLOWED_CONTENT_TYPES:
            raise HTTPException(
                status_code=415,
                detail=f"{file.filename or 'A file'} is not supported. Use JPG, PNG or WEBP images.",
            )
        contents = await file.read(MAX_IMAGE_BYTES + 1)
        if not contents:
            raise HTTPException(status_code=422, detail=f"{file.filename or 'An image'} is empty")
        if len(contents) > MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"{file.filename or 'An image'} exceeds the 8 MB limit",
            )
        file_contents.append((file, contents))

    configure_cloudinary()
    uploaded: list[dict] = []
    try:
        for file, contents in file_contents:
            result = cloudinary.uploader.upload(
                BytesIO(contents),
                resource_type="image",
                folder=f"rentkhoj/owners/{owner['_id']}/properties",
                allowed_formats=["jpg", "jpeg", "png", "webp"],
                unique_filename=True,
                overwrite=False,
            )
            secure_url = result.get("secure_url")
            if not secure_url or not secure_url.startswith("https://"):
                raise RuntimeError("Cloudinary did not return a secure image URL")
            uploaded.append({"url": secure_url, "public_id": result.get("public_id")})
    except Exception:
        # Avoid leaving already-uploaded images behind if this batch fails.
        for item in uploaded:
            public_id = item.get("public_id")
            if public_id:
                try:
                    cloudinary.uploader.destroy(public_id, resource_type="image")
                except Exception:
                    pass
        raise HTTPException(
            status_code=502,
            detail="One or more images could not be uploaded. Please try again.",
        )
    finally:
        for file in files:
            await file.close()

    return {"count": len(uploaded), "image_urls": [item["url"] for item in uploaded]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_property(
    data: PropertyCreate,
    owner: dict = Depends(require_owner),
):
    property_data = data.model_dump()
    if len(property_data.get("image_urls", [])) > MAX_IMAGES:
        raise HTTPException(status_code=422, detail="You can add a maximum of 10 photos")
    for url in property_data.get("image_urls", []):
        if not url.startswith("https://"):
            raise HTTPException(status_code=422, detail="Property image URLs must use HTTPS")

    now = datetime.now(timezone.utc)
    property_data.update({
        "owner_id": owner["_id"],
        "status": "pending",
        "created_at": now,
        "updated_at": now,
    })

    try:
        result = await properties_collection.insert_one(property_data)
        saved = await properties_collection.find_one({"_id": result.inserted_id})
        return serialize_property(saved)
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Unable to save property right now")


@router.get("/mine")
async def get_my_properties(owner: dict = Depends(require_owner)):
    cursor = properties_collection.find({"owner_id": owner["_id"]}).sort("created_at", -1)
    results = []
    async for item in cursor:
        results.append(serialize_property(item))
    return {"count": len(results), "properties": results}


@router.get("/{property_id}")
async def get_property(property_id: str):
    """Return one publicly visible listing and its owner's public contact details."""
    if not ObjectId.is_valid(property_id):
        raise HTTPException(status_code=404, detail="Property not found")

    try:
        item = await properties_collection.find_one({
            "_id": ObjectId(property_id),
            "status": {"$in": ["active", "approved"]},
        })
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Unable to fetch this property right now")

    if item is None:
        raise HTTPException(status_code=404, detail="Property not found")

    result = serialize_property(item)
    owner_id = item.get("owner_id")
    owner = None
    if isinstance(owner_id, ObjectId):
        owner = await users_collection.find_one({"_id": owner_id}, {"name": 1, "phone": 1})
    elif isinstance(owner_id, str) and ObjectId.is_valid(owner_id):
        owner = await users_collection.find_one({"_id": ObjectId(owner_id)}, {"name": 1, "phone": 1})
    result["owner_name"] = owner.get("name") if owner else None
    result["owner_phone"] = owner.get("phone") if owner else None
    return result
