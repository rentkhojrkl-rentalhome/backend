import csv
from io import BytesIO, StringIO

from bson import ObjectId
from fastapi import HTTPException
from fastapi.responses import Response

from app.database import db, favorites_collection, messages_collection, properties_collection, reports_collection, requirements_collection, rentals_collection, users_collection

EXPORT_COLLECTIONS = {
    "users": users_collection, "properties": properties_collection, "requirements": requirements_collection,
    "rentals": rentals_collection, "favorites": favorites_collection, "messages": messages_collection,
    "reports": reports_collection, "admin_activity": db["admin_activity"],
}
EXCLUDED_FIELDS = {"password_hash", "email_otp_hash", "password_reset_token_hash", "email_otp_expires_at", "email_otp_last_sent_at", "password_reset_expires_at"}

def value_for_export(value):
    if isinstance(value, ObjectId): return str(value)
    if isinstance(value, (list, dict)): return str(value)
    return value

async def build_export(collection_name: str, format_name: str) -> Response:
    collection = EXPORT_COLLECTIONS.get(collection_name)
    # AsyncMongoCollection deliberately does not support truth-value testing.
    # A missing allowlisted collection is represented by None.
    if collection is None:
        raise HTTPException(404, "Collection is not exportable")
    rows = []
    async for item in collection.find({}):
        rows.append({key: value_for_export(value) for key, value in item.items() if key not in EXCLUDED_FIELDS})
    fields = sorted({key for row in rows for key in row})
    filename = f"rentkhoj-{collection_name}-export.{format_name}"
    if format_name == "csv":
        output = StringIO(); writer = csv.DictWriter(output, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
        return Response(output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail="Excel exports require openpyxl. Run: pip install -r requirements.txt",
        ) from exc
    workbook = Workbook(); sheet = workbook.active; sheet.title = collection_name[:31]; sheet.append(fields)
    for row in rows: sheet.append([row.get(field, "") for field in fields])
    output = BytesIO(); workbook.save(output)
    return Response(output.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="{filename}"'})
