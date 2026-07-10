"""Promo-code redemption endpoint."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.db import promo as promo_db
from app.deps import get_current_user

router = APIRouter(tags=["promo"])


class RedeemRequest(BaseModel):
    code: str


@router.post("/promo/redeem")
def redeem_promo(body: RedeemRequest, user=Depends(get_current_user)):
    """Redeem a promo code for the authenticated user.

    Validation is entirely server-side (see app/db/promo.py). The client only
    submits the code string; it can neither read the codes table nor set its own
    tier. Returns {ok, tier, expires_at} or {ok: false, error}.
    """
    return promo_db.redeem_code(user.id, body.code)
