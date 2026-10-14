
import os

import certifi
from dotenv import load_dotenv
from pymongo import AsyncMongoClient

load_dotenv()

MONGO_URL = os.getenv("MONGO_URL")
DB_NAME = os.getenv("DB_NAME", "rentkhoj")

if not MONGO_URL:
    raise RuntimeError("MONGO_URL is missing from your .env file")

client = AsyncMongoClient(
    MONGO_URL,
    serverSelectionTimeoutMS=10000,
    # Atlas requires a trusted CA bundle for TLS connections. Using certifi
    # avoids dependence on a machine-specific Windows certificate store.
    tlsCAFile=certifi.where(),
)

db = client[DB_NAME]

users_collection = db["users"]
properties_collection = db["properties"]
favorites_collection = db["favorites"]
requirements_collection = db["requirements"]
messages_collection = db["messages"]
reports_collection = db["reports"]
