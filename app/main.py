
from contextlib import asynccontextmanager
import logging
import os

from bson import ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pymongo.errors import PyMongoError
from starlette.responses import JSONResponse

from app.database import client, db, properties_collection, users_collection
from app.auth import router as auth_router
from app.properties import router as properties_router
from app.admin import router as admin_router
from pydantic import EmailStr
from app.email_service import send_test_email
from app.notifications import router as notifications_router
from app.super_admin import router as super_admin_router
from app.requirements import router as requirements_router
from app.owner_verification import router as owner_verification_router
from app.rentals import router as rentals_router

load_dotenv()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await client.admin.command("ping")
        await users_collection.create_index("email", unique=True)
        logger.info("Connected to MongoDB database: %s", db.name)
        await db["owner_verification_requests"].create_index([("owner_id", 1), ("created_at", -1)])
        await db["owner_verification_requests"].create_index([("status", 1), ("created_at", -1)])
        await db["rentals"].create_index([("owner_id", 1),("created_at", -1),])
        await db["rentals"].create_index([ ("property_id", 1), ("status", 1),])
        await db["database_backups"].create_index([("created_at", -1),])
        
    except PyMongoError:
        # Keep the API process running so its health endpoint and error
        # responses remain available while Atlas/network access is restored.
        logger.exception("MongoDB is unavailable at startup")

    yield
    

    await client.close()


app = FastAPI(
    title="RentKhoj API",
    description="Rourkela Rental Marketplace API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(PyMongoError)
async def database_error_handler(_: Request, __: PyMongoError):
    """Avoid exposing database traces as opaque HTTP 500 responses."""
    return JSONResponse(
        status_code=503,
        content={"detail": "Database temporarily unavailable. Please try again shortly."},
    )

app.include_router(auth_router)
app.include_router(properties_router)
app.include_router(admin_router)
app.include_router(notifications_router)
app.include_router(requirements_router)
app.include_router(super_admin_router)
app.include_router(owner_verification_router)
app.include_router(rentals_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://rentkhojrkl.in",
        "https://www.rentkhojrkl.in",
        "https://rentkhojrkl.vercel.app",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {
        "message": "Welcome to RentKhoj API",
        "docs": "/docs",
    }


@app.get("/api/health")
async def health():
    try:
        await client.admin.command("ping")
        return {
            "status": "healthy",
            "database": "connected",
            "name": db.name,
        }
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database connection unavailable",
        )


def serialize_public_property(item: dict) -> dict:
    item["id"] = str(item.pop("_id"))

    # Convert MongoDB ObjectId values to JSON-safe strings.
    for key, value in item.items():
        if isinstance(value, ObjectId):
            item[key] = str(value)

    return item


@app.get("/api/properties")
async def get_properties():
    try:
        # Pending and rejected listings must never be publicly discoverable.
        cursor = properties_collection.find(
           {
    "status": {"$in": ["active", "approved"]},
    "rental_status": {"$ne": "rented"},
    "deleted_at": None,
}
        ).sort("created_at", -1).limit(100)

        properties = []

        async for item in cursor:
            properties.append(serialize_public_property(item))

        return {
            "count": len(properties),
            "properties": properties,
        }

    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Unable to fetch properties",
        )
@app.post("/api/test-email")
def test_email(to_email: EmailStr):
    try:
        result = send_test_email(str(to_email))
        return {"message": "Email sent", "id": result.get("id")}
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="Email could not be sent. Check server logs."
        )
