import asyncio
import os

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.deps import get_current_user

router = APIRouter(tags=["captcha"])

CAPSOLVER_BASE = "https://api.capsolver.com"
SOLVE_TIMEOUT_SEC = 120


@router.post("/captcha/solve")
async def solve_captcha(payload: dict, user=Depends(get_current_user)):
    api_key = os.getenv("CAPSOLVER_API_KEY")
    if not api_key:
        return JSONResponse(status_code=503, content={"error": "CAPTCHA solver not configured — set CAPSOLVER_API_KEY in Railway env"})

    captcha_type = payload.get("type")
    url = payload.get("url", "")

    if captcha_type == "recaptchav2":
        sitekey = payload.get("sitekey", "")
        if not sitekey:
            return JSONResponse(status_code=400, content={"error": "sitekey required for recaptchav2"})
        task = {
            "type": "ReCaptchaV2TaskProxyless",
            "websiteURL": url,
            "websiteKey": sitekey,
        }
    else:
        return JSONResponse(status_code=400, content={"error": f"Unsupported captcha type: {captcha_type}"})

    async with httpx.AsyncClient(timeout=30) as client:
        create_res = await client.post(f"{CAPSOLVER_BASE}/createTask", json={
            "clientKey": api_key,
            "task": task,
        })
        data = create_res.json()
        if data.get("errorId"):
            return JSONResponse(status_code=502, content={"error": data.get("errorDescription", "CapSolver error")})
        task_id = data.get("taskId")
        if not task_id:
            return JSONResponse(status_code=502, content={"error": "No taskId returned by CapSolver"})

        loop = asyncio.get_event_loop()
        deadline = loop.time() + SOLVE_TIMEOUT_SEC
        while loop.time() < deadline:
            await asyncio.sleep(3)
            result_res = await client.post(f"{CAPSOLVER_BASE}/getTaskResult", json={
                "clientKey": api_key,
                "taskId": task_id,
            })
            result = result_res.json()
            if result.get("errorId"):
                return JSONResponse(status_code=502, content={"error": result.get("errorDescription", "CapSolver error")})
            if result.get("status") == "ready":
                token = result.get("solution", {}).get("gRecaptchaResponse")
                if not token:
                    return JSONResponse(status_code=502, content={"error": "No token in CapSolver solution"})
                return {"token": token}

    return JSONResponse(status_code=504, content={"error": "CAPTCHA solve timeout (120s) — CapSolver did not return a result"})
