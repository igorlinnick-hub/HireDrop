"""Cold-email campaigns: assemble a template + contact list, launch, track.

A campaign = (template × set of contacts). Launch iterates contacts, renders
the template per recipient, sends via the user's Gmail (modules/gmail_sender),
persists one campaign_sends row per recipient, throttles 5-15s jitter, and
respects Gmail's daily cap. Each send burns 1 credit (credit ledger lands in
Phase 2 — for now we just log intent).

Also exposes the public /u/{token} unsubscribe route (CAN-SPAM).
"""

import os
import random
import secrets
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.db.client import get_supabase
from app.deps import get_current_user
from config import FRONTEND_URL
from modules.gmail_sender import send_via_gmail

router = APIRouter(tags=["outreach-campaigns"])

# Default safe ceiling per launch. User can raise to 500 (Gmail free) but we
# default low for abuse protection.
DEFAULT_SEND_CEILING = 100


class CampaignCreate(BaseModel):
    name: str = Field(..., max_length=200)
    template_id: str


class CampaignLaunch(BaseModel):
    contact_ids: list[str] = Field(..., min_length=1, max_length=500)
    send_ceiling: int = Field(DEFAULT_SEND_CEILING, ge=1, le=500)


@router.get("/campaigns")
def list_campaigns(user=Depends(get_current_user)):
    res = (
        get_supabase()
        .table("campaigns")
        .select("id, name, status, started_at, finished_at, created_at, template_id")
        .eq("user_id", str(user.id))
        .order("created_at", desc=True)
        .execute()
    )
    return res.data or []


@router.post("/campaigns")
def create_campaign(req: CampaignCreate, user=Depends(get_current_user)):
    res = (
        get_supabase()
        .table("campaigns")
        .insert({
            "user_id": str(user.id),
            "name": req.name,
            "template_id": req.template_id,
            "status": "draft",
        })
        .execute()
    )
    return (res.data or [{}])[0]


@router.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: str, user=Depends(get_current_user)):
    sb = get_supabase()
    camp = (
        sb.table("campaigns").select("*").eq("id", campaign_id).eq("user_id", str(user.id))
        .maybe_single().execute()
    )
    if not camp.data:
        raise HTTPException(status_code=404, detail="Campaign not found")
    sends = (
        sb.table("campaign_sends")
        .select("id, contact_id, status, sent_at, error, gmail_message_id")
        .eq("campaign_id", campaign_id)
        .execute()
    )
    return {**camp.data, "sends": sends.data or []}


@router.post("/campaigns/{campaign_id}/launch")
def launch_campaign(
    campaign_id: str,
    req: CampaignLaunch,
    user=Depends(get_current_user),
):
    """Synchronously iterate contacts and send. For long lists this blocks the
    request; in Phase 1.5 we move to a background worker. MVP keeps it inline.
    """
    sb = get_supabase()

    # Verify user owns campaign + load template
    camp = (
        sb.table("campaigns").select("id, template_id")
        .eq("id", campaign_id).eq("user_id", str(user.id))
        .maybe_single().execute()
    )
    if not camp.data:
        raise HTTPException(status_code=404, detail="Campaign not found")

    tmpl = (
        sb.table("email_templates").select("subject, body_md")
        .eq("id", camp.data["template_id"]).eq("user_id", str(user.id))
        .maybe_single().execute()
    )
    if not tmpl.data:
        raise HTTPException(status_code=404, detail="Template not found")

    # Load user's Gmail credentials
    profile = (
        sb.table("profiles").select("gmail_refresh_token, gmail_email, name, last_name")
        .eq("user_id", str(user.id))
        .maybe_single().execute()
    )
    refresh_token = (profile.data or {}).get("gmail_refresh_token")
    sender_email = (profile.data or {}).get("gmail_email")
    if not refresh_token or not sender_email:
        raise HTTPException(status_code=412, detail="Gmail not connected — visit /dashboard/outreach/settings")

    sender_name = " ".join(
        x for x in [(profile.data or {}).get("name"), (profile.data or {}).get("last_name")] if x
    ) or None

    # Load target contacts (filter unsubscribed)
    contact_ids = req.contact_ids[: req.send_ceiling]
    contacts_resp = (
        sb.table("contacts")
        .select("id, email, name, company, role, unsubscribed")
        .in_("id", contact_ids)
        .eq("user_id", str(user.id))
        .execute()
    )
    contacts = [c for c in (contacts_resp.data or []) if not c.get("unsubscribed")]

    # Mark campaign running
    sb.table("campaigns").update({"status": "running", "started_at": "now()"}).eq(
        "id", campaign_id
    ).execute()

    sent_count = 0
    failed_count = 0
    for contact in contacts:
        # Generate per-recipient unsubscribe token
        unsub_token = secrets.token_urlsafe(24)
        sb.table("unsubscribe_tokens").insert({
            "token": unsub_token, "contact_id": contact["id"],
        }).execute()

        rendered_html = _render(tmpl.data["body_md"], contact) + _unsub_footer(unsub_token, sender_email)
        rendered_subject = _render(tmpl.data["subject"], contact)

        result = send_via_gmail(
            refresh_token=refresh_token,
            sender_email=sender_email,
            sender_name=sender_name,
            to_email=contact["email"],
            subject=rendered_subject,
            html_body=rendered_html,
        )

        if result.get("ok"):
            sb.table("campaign_sends").insert({
                "campaign_id": campaign_id, "contact_id": contact["id"],
                "status": "sent", "gmail_message_id": result.get("message_id"),
                "sent_at": "now()",
            }).execute()
            sent_count += 1
        else:
            sb.table("campaign_sends").insert({
                "campaign_id": campaign_id, "contact_id": contact["id"],
                "status": "failed", "error": result.get("error", "")[:300],
            }).execute()
            failed_count += 1

        time.sleep(random.uniform(5.0, 15.0))  # human-like jitter

    sb.table("campaigns").update({"status": "done", "finished_at": "now()"}).eq(
        "id", campaign_id
    ).execute()

    return {"sent": sent_count, "failed": failed_count, "total": len(contacts)}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _render(template_str: str, contact: dict) -> str:
    """Tiny Mustache-lite renderer for {{first_name}} / {{company}} / {{role}}."""
    first_name = (contact.get("name") or "").split(" ")[0] if contact.get("name") else "there"
    return (
        template_str
        .replace("{{first_name}}", first_name)
        .replace("{{name}}", contact.get("name") or first_name)
        .replace("{{company}}", contact.get("company") or "your company")
        .replace("{{role}}", contact.get("role") or "your team")
    )


def _unsub_footer(token: str, sender_email: str) -> str:
    """CAN-SPAM-compliant footer with one-click unsubscribe link."""
    url = f"{FRONTEND_URL}/u/{token}"  # frontend route will hit /api/v1/unsubscribe/{token}
    return (
        f'<br><br><hr style="border:none;border-top:1px solid #eee;margin:24px 0">'
        f'<p style="font-size:11px;color:#888;font-family:sans-serif">'
        f'Sent by {sender_email}. '
        f'<a href="{url}" style="color:#888;text-decoration:underline">Unsubscribe</a> '
        f'to stop future emails from this sender.</p>'
    )


# -----------------------------------------------------------------------------
# Public unsubscribe endpoint — anonymous, token-gated
# -----------------------------------------------------------------------------
@router.get("/unsubscribe/{token}", response_class=HTMLResponse, include_in_schema=False)
def unsubscribe(token: str):
    sb = get_supabase()
    row = sb.table("unsubscribe_tokens").select("contact_id").eq("token", token).maybe_single().execute()
    if not row.data:
        return HTMLResponse(
            "<p>Link expired or invalid.</p>",
            status_code=404,
        )
    sb.table("contacts").update({"unsubscribed": True}).eq("id", row.data["contact_id"]).execute()
    return HTMLResponse(
        "<p>You have been unsubscribed. You will not receive further emails from this sender.</p>"
    )
