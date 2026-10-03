"""The auth dependency must stay off the event loop.

get_current_user makes blocking network calls (supabase-py is sync). Declared
`async def`, FastAPI ran it on the event loop and every token check froze the
whole worker; as plain `def` it runs in the threadpool. Measured: 8 concurrent
requests with a 0.3 s check took 2.45 s async vs 0.31 s def.
"""

import asyncio
import inspect
import time
import types

import httpx
from fastapi import Depends, FastAPI

import app.deps as deps


def test_get_current_user_is_sync_so_fastapi_threadpools_it():
    assert not inspect.iscoroutinefunction(deps.get_current_user)


def test_concurrent_token_checks_overlap(monkeypatch):
    class SlowAuth:
        def get_user(self, token):
            time.sleep(0.3)  # stands in for the blocking HTTP call to Supabase
            return types.SimpleNamespace(user=types.SimpleNamespace(id="u1", email="a@b.c"))

    monkeypatch.setattr(deps, "get_supabase", lambda: types.SimpleNamespace(auth=SlowAuth()))

    app = FastAPI()

    @app.get("/probe")
    def probe(user=Depends(deps.get_current_user)):
        return {"id": user.id}

    async def fire():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            start = time.perf_counter()
            responses = await asyncio.gather(
                *[client.get("/probe", headers={"Authorization": "Bearer x"}) for _ in range(6)]
            )
            return time.perf_counter() - start, responses

    elapsed, responses = asyncio.run(fire())
    assert all(r.status_code == 200 for r in responses)
    # Serialised would be 6 × 0.3 = 1.8 s. Generous ceiling for a slow CI box.
    assert elapsed < 1.2, f"token checks ran in series ({elapsed:.2f}s)"
