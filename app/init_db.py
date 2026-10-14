
import asyncio
from datetime import datetime, timezone

from app.database import client, db


async def main():
    try:
        await client.admin.command("ping")

        result = await db["health_checks"].update_one(
            {"_id": "rentkhoj-backend-test"},
            {
                "$set": {
                    "message": "RentKhoj database connection works",
                    "checked_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )

        saved = await db["health_checks"].find_one(
            {"_id": "rentkhoj-backend-test"}
        )

        print("MongoDB connection: SUCCESS")
        print("Database:", db.name)
        print("Test record saved:", bool(saved))
        print("Record:", saved)

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
