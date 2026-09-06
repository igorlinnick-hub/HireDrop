"""Interview kits — storage owner for the interview_kits table.

One cached prep sheet per (user, application). Generation costs an AI call, so the kit is
written once and read many times: the user opens it before the call, during the call, and
again before the next round.
"""

from app.db.client import get_supabase


def get_kit(user_id: str, application_id: str) -> dict | None:
    """The cached kit for one application, or None.

    Scoped by user_id as well as application_id — the backend runs under service_role and
    bypasses RLS, so this filter is the only thing standing between two tenants.
    """
    res = (
        get_supabase()
        .table("interview_kits")
        .select("*")
        .eq("user_id", user_id)
        .eq("application_id", application_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def save_kit(
    user_id: str, application_id: str, payload: dict, model: str, schema_version: int
) -> dict | None:
    res = (
        get_supabase()
        .table("interview_kits")
        .upsert(
            {
                "user_id": user_id,
                "application_id": application_id,
                "payload": payload,
                "model": model,
                "schema_version": schema_version,
            },
            on_conflict="user_id,application_id",
        )
        .execute()
    )
    return res.data[0] if res.data else None
