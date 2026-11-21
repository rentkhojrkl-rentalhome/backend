
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pymongo.errors import PyMongoError

from app.auth import get_current_user
from app.database import notifications_collection, users_collection

router = APIRouter(
    prefix="/api/notifications",
    tags=["Notifications"]
)

ALLOWED_TYPES = {
    "general",
    "property_submitted",
    "property_approved",
    "property_rejected",
    "owner_verification",
    "rental_update",
    "requirement_match",
}


def safe_link(link: str | None) -> str | None:
    if link is None:
        return None

    if (
        not link.startswith("/")
        or link.startswith("//")
        or "\\" in link
    ):
        raise ValueError("Only relative application links are allowed")

    return link


def serialize_notification(item: dict) -> dict:
    item = dict(item)
    item["id"] = str(item.pop("_id"))
    item["recipient_id"] = str(item["recipient_id"])
    return item


async def create_notification(
    recipient_id: ObjectId | str,
    title: str,
    message: str,
    *,
    notification_type: str = "general",
    link: str | None = None,
    data: dict[str, Any] | None = None,
) -> str | None:

    if isinstance(recipient_id, str):
        if not ObjectId.is_valid(recipient_id):
            raise ValueError("Invalid recipient ID")
        recipient_id = ObjectId(recipient_id)

    if not isinstance(recipient_id, ObjectId):
        raise ValueError("Invalid recipient ID")

    if notification_type not in ALLOWED_TYPES:
        notification_type = "general"

    title = title.strip()
    message = message.strip()

    if not title or not message:
        raise ValueError("Title and message are required")

    document = {
        "recipient_id": recipient_id,
        "title": title[:120],
        "message": message[:1000],
        "type": notification_type,
        "link": safe_link(link),
        "data": data or {},
        "is_read": False,
        "created_at": datetime.now(timezone.utc),
        "read_at": None,
    }

    result = await notifications_collection.insert_one(document)
    return str(result.inserted_id)


async def notify_platform_admins(
    title: str,
    message: str,
    *,
    notification_type: str = "general",
    link: str | None = None,
    data: dict[str, Any] | None = None,
) -> int:

    cursor = users_collection.find({
        "role": {"$in": ["admin", "super_admin"]},
        "is_active": {"$ne": False},
        "deleted_at": None,
        "email_verified": True,
    })

    count = 0

    async for user in cursor:
        await create_notification(
            user["_id"],
            title,
            message,
            notification_type=notification_type,
            link=link,
            data=data,
        )
        count += 1

    return count


# GET NOTIFICATIONS
@router.get("")
async def get_notifications(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    unread_only: bool = False,
    user: dict = Depends(get_current_user),
):

    query = {"recipient_id": user["_id"]}

    if unread_only:
        query["is_read"] = False

    try:
        total = await notifications_collection.count_documents(query)

        cursor = (
            notifications_collection.find(query)
            .sort("created_at", -1)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )

        notifications = [
            serialize_notification(item)
            async for item in cursor
        ]

        unread_count = await notifications_collection.count_documents({
            "recipient_id": user["_id"],
            "is_read": False,
        })

    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Notifications temporarily unavailable",
        )

    return {
        "notifications": notifications,
        "total": total,
        "page": page,
        "page_size": page_size,
        "unread_count": unread_count,
    }


# UNREAD COUNT
@router.get("/unread-count")
async def unread_count(
    user: dict = Depends(get_current_user),
):

    count = await notifications_collection.count_documents({
        "recipient_id": user["_id"],
        "is_read": False,
    })

    return {"unread_count": count}


# MARK ALL AS READ
@router.patch("/read-all")
async def mark_all_read(
    user: dict = Depends(get_current_user),
):

    result = await notifications_collection.update_many(
        {
            "recipient_id": user["_id"],
            "is_read": False,
        },
        {
            "$set": {
                "is_read": True,
                "read_at": datetime.now(timezone.utc),
            }
        },
    )

    return {
        "message": "All notifications marked as read",
        "updated_count": result.modified_count,
    }


# MARK ONE AS READ
@router.patch("/{notification_id}/read")
async def mark_one_read(
    notification_id: str,
    user: dict = Depends(get_current_user),
):

    if not ObjectId.is_valid(notification_id):
        raise HTTPException(404, "Notification not found")

    item = await notifications_collection.find_one_and_update(
        {
            "_id": ObjectId(notification_id),
            "recipient_id": user["_id"],
        },
        {
            "$set": {
                "is_read": True,
                "read_at": datetime.now(timezone.utc),
            }
        },
        return_document=True,
    )

    if not item:
        raise HTTPException(404, "Notification not found")

    return {
        "notification": serialize_notification(item)
    }


# DELETE NOTIFICATION
@router.delete("/{notification_id}")
async def delete_notification(
    notification_id: str,
    user: dict = Depends(get_current_user),
):

    if not ObjectId.is_valid(notification_id):
        raise HTTPException(404, "Notification not found")

    result = await notifications_collection.delete_one({
        "_id": ObjectId(notification_id),
        "recipient_id": user["_id"],
    })

    if result.deleted_count != 1:
        raise HTTPException(404, "Notification not found")

    return {"message": "Notification deleted"}
