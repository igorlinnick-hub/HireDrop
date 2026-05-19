"""Email templates for cold-email outreach campaigns.

CRUD over `email_templates` table. Each template has a subject + body_md.
Body supports `{{first_name}}`, `{{company}}`, `{{role}}` placeholders that
get rendered per recipient at send time.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db.client import get_supabase
from app.deps import get_current_user

router = APIRouter(tags=["outreach-templates"])


class TemplateCreate(BaseModel):
    name: str = Field(..., max_length=120)
    subject: str = Field(..., max_length=200)
    body_md: str = Field(..., max_length=20000)


class TemplateUpdate(BaseModel):
    name: str | None = Field(None, max_length=120)
    subject: str | None = Field(None, max_length=200)
    body_md: str | None = Field(None, max_length=20000)


@router.get("/templates")
def list_templates(user=Depends(get_current_user)):
    res = (
        get_supabase()
        .table("email_templates")
        .select("id, name, subject, body_md, created_at, updated_at")
        .eq("user_id", str(user.id))
        .order("updated_at", desc=True)
        .execute()
    )
    return res.data or []


@router.post("/templates")
def create_template(req: TemplateCreate, user=Depends(get_current_user)):
    res = (
        get_supabase()
        .table("email_templates")
        .insert({
            "user_id": str(user.id),
            "name": req.name,
            "subject": req.subject,
            "body_md": req.body_md,
        })
        .execute()
    )
    return (res.data or [{}])[0]


@router.get("/templates/{template_id}")
def get_template(template_id: str, user=Depends(get_current_user)):
    res = (
        get_supabase()
        .table("email_templates")
        .select("*")
        .eq("id", template_id)
        .eq("user_id", str(user.id))
        .maybe_single()
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Template not found")
    return res.data


@router.patch("/templates/{template_id}")
def update_template(template_id: str, req: TemplateUpdate, user=Depends(get_current_user)):
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    if not patch:
        raise HTTPException(status_code=400, detail="No fields to update")
    patch["updated_at"] = "now()"
    res = (
        get_supabase()
        .table("email_templates")
        .update(patch)
        .eq("id", template_id)
        .eq("user_id", str(user.id))
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Template not found")
    return res.data[0]


@router.delete("/templates/{template_id}")
def delete_template(template_id: str, user=Depends(get_current_user)):
    get_supabase().table("email_templates").delete().eq("id", template_id).eq(
        "user_id", str(user.id)
    ).execute()
    return {"deleted": True, "id": template_id}


# -----------------------------------------------------------------------------
# AI assist — Anthropic-generated template body from a niche brief
# -----------------------------------------------------------------------------
# Not implemented yet (Phase 1.3 follow-up). Wire to modules/ai_cover_letter.py
# or a dedicated modules/ai_outreach.py once the credit ledger lands.
# @router.post("/templates/{template_id}/generate") — costs 2 credits.
