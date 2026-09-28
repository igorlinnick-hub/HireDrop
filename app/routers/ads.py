"""Ad-spend ingest — the one door spend data from outside comes in through.

POST /admin/ads/spend is called by the Google Ads Script
(scripts/google_ads_spend.js) once a day. Meta spend does not come through
here: the board pulls it from the Insights API itself (app/ads/meta_spend.py),
so each platform has exactly one writer.

Guarded by its own secret (ADS_INGEST_TOKEN, header X-Ads-Ingest-Token), not
by ADMIN_METRICS_TOKEN: the ingest token lives inside a Google Ads account, and
leaking it must not open the admin board. Unset -> 503, never open.
"""

import hmac
import os
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.db import ad_spend as spend_db

router = APIRouter(prefix="/admin/ads", tags=["admin"])

MAX_ROWS = 5000


def require_ingest_token(
    token: str | None = Header(default=None, alias="X-Ads-Ingest-Token"),
) -> None:
    """A dependency, not a call inside the handler: FastAPI resolves it BEFORE
    validating the body, so a caller without the token learns 401, not our schema."""
    expected = os.getenv("ADS_INGEST_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Ad spend ingest not configured")
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid ingest token")


class SpendRow(BaseModel):
    date: date
    campaign_id: str | None = Field(default=None, max_length=64)
    campaign_name: str | None = Field(default=None, max_length=300)
    ad_id: str | None = Field(default=None, max_length=64)
    ad_name: str | None = Field(default=None, max_length=300)
    spend_usd: float = Field(ge=0, le=100_000)
    impressions: int | None = Field(default=None, ge=0)
    clicks: int | None = Field(default=None, ge=0)


class SpendIngest(BaseModel):
    # Meta is owned by the Insights sync; accepting it here would give that
    # platform two writers that overwrite each other.
    platform: Literal["google", "manual"]
    # Google: the Ads customer id. Manual: the utm_source this spend bought
    # (e.g. "reddit") — that is how the board matches it to signups.
    account_id: str | None = Field(default=None, max_length=64)
    rows: list[SpendRow] = Field(max_length=MAX_ROWS)


def _ad_key(row: SpendRow) -> str | None:
    """The row's ad_id, or a synthetic per-campaign key when the source has no ad."""
    if row.ad_id and row.ad_id.strip():
        return row.ad_id.strip()
    campaign = (row.campaign_id or row.campaign_name or "").strip()
    return f"campaign:{campaign}" if campaign else None


@router.post("/spend", dependencies=[Depends(require_ingest_token)])
def ingest_spend(body: SpendIngest) -> dict:
    rows = []
    for i, r in enumerate(body.rows):
        key = _ad_key(r)
        if not key:
            raise HTTPException(
                status_code=422, detail=f"rows[{i}]: needs ad_id or campaign_id/campaign_name"
            )
        rows.append(
            {
                "date": r.date.isoformat(),
                "platform": body.platform,
                "account_id": body.account_id,
                "campaign_id": r.campaign_id,
                "campaign_name": r.campaign_name,
                "ad_id": key,
                "ad_name": r.ad_name,
                "spend_usd": round(r.spend_usd, 2),
                "impressions": r.impressions,
                "clicks": r.clicks,
                "source": f"{body.platform}_ingest",
            }
        )
    written = spend_db.upsert(rows) if rows else 0
    return {"upserted": written, "received": len(body.rows)}
