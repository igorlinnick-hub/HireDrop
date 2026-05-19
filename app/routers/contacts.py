"""Recruiter/contact list management for cold-email outreach.

Sources: CSV upload + manual entry only. NO scraping (LinkedIn TOS + privacy).
"""

import csv
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, EmailStr, Field

from app.db.client import get_supabase
from app.deps import get_current_user

router = APIRouter(tags=["outreach-contacts"])


class ContactCreate(BaseModel):
    email: EmailStr
    name: str | None = Field(None, max_length=200)
    company: str | None = Field(None, max_length=200)
    role: str | None = Field(None, max_length=200)
    tags: list[str] = []


@router.get("/contacts")
def list_contacts(user=Depends(get_current_user), limit: int = 500):
    res = (
        get_supabase()
        .table("contacts")
        .select("id, email, name, company, role, source, tags, unsubscribed, created_at")
        .eq("user_id", str(user.id))
        .order("created_at", desc=True)
        .limit(min(limit, 2000))
        .execute()
    )
    return res.data or []


@router.post("/contacts")
def create_contact(req: ContactCreate, user=Depends(get_current_user)):
    try:
        res = (
            get_supabase()
            .table("contacts")
            .insert({
                "user_id": str(user.id),
                "email": req.email.lower(),
                "name": req.name,
                "company": req.company,
                "role": req.role,
                "source": "manual",
                "tags": req.tags,
            })
            .execute()
        )
    except Exception as e:
        # Likely unique-index violation on (user_id, lower(email))
        raise HTTPException(status_code=409, detail=f"Could not create contact: {str(e)[:100]}")
    return (res.data or [{}])[0]


@router.post("/contacts/import")
async def import_contacts(
    file: UploadFile = File(...), user=Depends(get_current_user)
):
    """Upload a CSV with columns: email, name, company, role, tags.
    Tags can be `tag1;tag2`. Dedupes by (user_id, lower(email)).
    """
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv uploads are accepted")

    raw = await file.read()
    if len(raw) > 2_000_000:  # 2 MB cap
        raise HTTPException(status_code=413, detail="CSV too large (>2MB)")

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV must be UTF-8")

    reader = csv.DictReader(io.StringIO(text))
    rows = []
    skipped = 0
    for row in reader:
        email = (row.get("email") or "").strip().lower()
        if not email or "@" not in email:
            skipped += 1
            continue
        tags_raw = (row.get("tags") or "").strip()
        tags = [t.strip() for t in tags_raw.split(";") if t.strip()] if tags_raw else []
        rows.append({
            "user_id": str(user.id),
            "email": email,
            "name": (row.get("name") or "").strip() or None,
            "company": (row.get("company") or "").strip() or None,
            "role": (row.get("role") or "").strip() or None,
            "source": "csv",
            "tags": tags,
        })

    if not rows:
        return {"inserted": 0, "skipped": skipped}

    # upsert: skip duplicates without raising
    sb = get_supabase()
    inserted = 0
    for batch_start in range(0, len(rows), 500):
        batch = rows[batch_start : batch_start + 500]
        try:
            res = sb.table("contacts").upsert(
                batch, on_conflict="user_id,email", ignore_duplicates=True
            ).execute()
            inserted += len(res.data or [])
        except Exception as e:
            print(f"[contacts/import] batch failed: {e}")
            skipped += len(batch)

    return {"inserted": inserted, "skipped": skipped, "total_rows": len(rows) + skipped}


@router.delete("/contacts/{contact_id}")
def delete_contact(contact_id: str, user=Depends(get_current_user)):
    get_supabase().table("contacts").delete().eq("id", contact_id).eq(
        "user_id", str(user.id)
    ).execute()
    return {"deleted": True, "id": contact_id}
