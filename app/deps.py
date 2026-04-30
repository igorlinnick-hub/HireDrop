"""Auth dependency — верификация Supabase JWT."""

from fastapi import Header, HTTPException

from app.db.client import get_supabase


async def get_current_user(authorization: str = Header(...)):
    """Верифицирует Bearer токен через Supabase и возвращает user объект."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header format")
    token = authorization.split(" ", 1)[1]
    try:
        response = get_supabase().auth.get_user(token)
        if not response or not response.user:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return response.user
    except HTTPException:
        raise
    except Exception as err:
        raise HTTPException(status_code=401, detail="Token verification failed") from err
