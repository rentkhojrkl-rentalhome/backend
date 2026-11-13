from contextlib import asynccontextmanager
import logging
import os

from bson import ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pymongo.errors import PyMongoError
from starlette.responses import JSONResponse, Response

from app.database import client, db, properties_collection, users_collection
from app.auth import router as auth_router
from app.properties import router as properties_router
from app.admin import router as admin_router
from app.requirements import router as requirements_router
from xml.etree.ElementTree import Element, SubElement, tostring
from app.super_admin import router as super_admin_router

load_dotenv()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await client.admin.command("ping")
        await users_collection.create_index("email", unique=True)
        await db["requirements"].create_index([("tenant_id", 1), ("created_at", -1)])
        await db["requirements"].create_index([("status", 1), ("created_at", -1)])
        logger.info("Connected to MongoDB database: %s", db.name)
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
app.include_router(requirements_router)
app.include_router(super_admin_router)

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
                "status": {
                    "$in": ["active", "approved"]
                }
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


@app.get("/api/sitemap.xml")
async def get_sitemap():
    base_url = "https://www.rentkhojrkl.in"

    # Create XML sitemap
    urlset = Element(
        "urlset",
        xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
    )

    # Fixed public pages
    static_pages = [
        ("/", "weekly", "1.0"),
        ("/search", "daily", "0.9"),
        ("/about", "monthly", "0.6"),
        ("/contact", "monthly", "0.5"),
    ]

    for path, frequency, priority in static_pages:
        url = SubElement(urlset, "url")
        SubElement(url, "loc").text = base_url + path
        SubElement(url, "changefreq").text = frequency
        SubElement(url, "priority").text = priority

    # Fetch all public properties
    cursor = properties_collection.find(
        {
            "status": {
                "$in": ["active", "approved"]
            }
        },
        {
            "_id": 1,
            "updated_at": 1,
            "created_at": 1,
        }
    )

    async for item in cursor:
        property_id = str(item["_id"])

        url = SubElement(urlset, "url")

        SubElement(url, "loc").text = (
            f"{base_url}/properties/{property_id}"
        )

        # Add last modified date
        last_modified = (
            item.get("updated_at")
            or item.get("created_at")
        )

        if last_modified:
            if hasattr(last_modified, "date"):
                last_modified = last_modified.date()

            SubElement(url, "lastmod").text = (
                last_modified.isoformat()
            )

        SubElement(url, "changefreq").text = "weekly"
        SubElement(url, "priority").text = "0.8"

    # Convert XML to bytes
    xml_content = tostring(
        urlset,
        encoding="utf-8",
        xml_declaration=True
    )

    return Response(
        content=xml_content,
        media_type="application/xml"
    )
