import asyncio
import json
import os
from playwright.async_api import async_playwright
from modules.ai_cover_letter import generate_cover_letter
from database.db import update_job_status, save_application

DAILY_LIMIT = 50
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
PLATFORM_NAMES = {"remoteok": "RemoteOK", "indeed": "Indeed", "wellfound": "Wellfound"}

PLATFORM_LOGIN_URLS = {
    "remoteok": "https://remoteok.com/login",
    "indeed": "https://secure.indeed.com/auth",
    "wellfound": "https://wellfound.com/login",
}

PLATFORM_SUCCESS_INDICATORS = {
    "remoteok": {"url_contains": ["remoteok.com"], "url_not_contains": ["/login"], "content_has": ["logout", "profile", "account"]},
    "indeed": {"url_contains": ["indeed.com"], "url_not_contains": ["/auth", "/login"], "content_has": ["profile", "account", "dashboard"]},
    "wellfound": {"url_contains": ["wellfound.com"], "url_not_contains": ["/login"], "content_has": ["profile", "dashboard", "logout"]},
}


def cookies_path(platform):
    return os.path.join(DATA_DIR, f"cookies_{platform}.json")


def save_cookies(platform, cookies):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(cookies_path(platform), "w") as f:
        json.dump(cookies, f, indent=2)


def load_cookies(platform):
    path = cookies_path(platform)
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return None


def delete_cookies(platform):
    path = cookies_path(platform)
    if os.path.exists(path):
        os.remove(path)


def is_platform_connected(platform):
    return os.path.exists(cookies_path(platform))


async def connect_platform_interactive(platform):
    """
    Open a visible browser for the user to log in manually.
    Waits for successful login, saves cookies, closes browser.
    Returns True if login succeeded.
    """
    login_url = PLATFORM_LOGIN_URLS.get(platform)
    if not login_url:
        return False

    indicators = PLATFORM_SUCCESS_INDICATORS.get(platform, {})

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(login_url)

        # Poll for successful login (check every 2 seconds, timeout 5 minutes)
        for _ in range(150):
            await asyncio.sleep(2)

            try:
                current_url = page.url.lower()

                # Check if we're no longer on the login page
                left_login = True
                for blocked in indicators.get("url_not_contains", []):
                    if blocked in current_url:
                        left_login = False
                        break

                if left_login:
                    # Verify we're on the right domain
                    on_domain = any(d in current_url for d in indicators.get("url_contains", []))
                    if on_domain:
                        # Double check with page content
                        content = (await page.content()).lower()
                        has_indicator = any(kw in content for kw in indicators.get("content_has", []))
                        if has_indicator:
                            # Login successful — save cookies
                            cookies = await context.cookies()
                            save_cookies(platform, cookies)
                            await browser.close()
                            return True
            except Exception:
                # Page might be navigating, keep waiting
                continue

        # Timeout — user didn't complete login
        await browser.close()
        return False


async def verify_cookies(platform):
    """
    Check if saved cookies are still valid by loading them and visiting the platform.
    Returns True if session is still active.
    """
    cookies = load_cookies(platform)
    if not cookies:
        return False

    indicators = PLATFORM_SUCCESS_INDICATORS.get(platform, {})
    login_url = PLATFORM_LOGIN_URLS.get(platform, "")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            await context.add_cookies(cookies)
            page = await context.new_page()

            # Visit the platform's main page
            domain = login_url.split("/login")[0].split("/auth")[0]
            await page.goto(domain, timeout=10000)
            await page.wait_for_timeout(3000)

            current_url = page.url.lower()
            content = (await page.content()).lower()

            await browser.close()

            # If redirected to login, cookies are expired
            for blocked in indicators.get("url_not_contains", []):
                if blocked in current_url:
                    delete_cookies(platform)
                    return False

            # Check for logged-in indicators
            has_indicator = any(kw in content for kw in indicators.get("content_has", []))
            if not has_indicator:
                delete_cookies(platform)
                return False

            return True

    except Exception:
        return False


async def apply_to_jobs(jobs, profile, callback):
    """
    Apply to jobs using Playwright with saved session cookies.
    Streams events via callback(event_dict).
    """
    new_jobs = [j for j in jobs if j["status"] == "new"]
    if not new_jobs:
        await callback({"type": "done", "message": "No new jobs to apply to"})
        return

    to_apply = new_jobs[:DAILY_LIMIT]
    total = len(to_apply)

    await callback({
        "type": "start", "total": total,
        "message": f"Starting application process for {total} jobs..."
    })
    await asyncio.sleep(0.3)

    # Group by platform
    platforms_order = {}
    for job in to_apply:
        p = job["platform"] if "platform" in job.keys() else "remoteok"
        if p not in platforms_order:
            platforms_order[p] = []
        platforms_order[p].append(job)

    applied_count = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        for platform_key, platform_jobs in platforms_order.items():
            pname = PLATFORM_NAMES.get(platform_key, platform_key.upper())

            await callback({
                "type": "platform_start", "platform": pname,
                "count": len(platform_jobs),
                "message": f"--- Starting {pname} ({len(platform_jobs)} jobs) ---"
            })
            await asyncio.sleep(0.5)

            # Load saved cookies for this platform
            cookies = load_cookies(platform_key)
            context = await browser.new_context()
            logged_in = False

            if cookies:
                try:
                    await context.add_cookies(cookies)
                    logged_in = True
                    await callback({
                        "type": "generating", "platform": pname,
                        "message": f"[{pname}] Session restored from saved cookies"
                    })
                except Exception as e:
                    await callback({
                        "type": "error", "platform": pname,
                        "message": f"[{pname}] Cookie restore failed: {str(e)[:60]} - generating letters only"
                    })
            else:
                await callback({
                    "type": "error", "platform": pname,
                    "message": f"[{pname}] Not connected - generating letters only"
                })

            page = await context.new_page()

            for job in platform_jobs:
                title = job["title"]
                company = job["company"]

                await callback({
                    "type": "generating", "platform": pname,
                    "job_id": job["id"],
                    "message": f"[{pname}] Generating cover letter for {title} @ {company}..."
                })
                await asyncio.sleep(0.2)

                try:
                    letter = generate_cover_letter({
                        "title": title,
                        "company": company,
                        "description": job["description"] if "description" in job.keys() else "",
                    }, profile)

                    actually_applied = False
                    if logged_in:
                        try:
                            actually_applied = await _submit_application(
                                page, platform_key, job, letter, profile
                            )
                        except Exception as e:
                            await callback({
                                "type": "error", "platform": pname,
                                "job_id": job["id"],
                                "message": f"[{pname}] Submit failed for {title}: {str(e)[:60]}"
                            })

                    update_job_status(job["id"], "applied")
                    save_application(job["id"], letter)
                    applied_count += 1

                    status = "Applied" if actually_applied else "Letter ready"
                    await callback({
                        "type": "applied", "platform": pname,
                        "job_id": job["id"], "title": title, "company": company,
                        "applied": applied_count, "total": total,
                        "message": f"[{pname}] {status}: {title} @ {company}"
                    })
                    await asyncio.sleep(2.0)

                except Exception as e:
                    await callback({
                        "type": "error", "platform": pname,
                        "job_id": job["id"],
                        "message": f"[{pname}] Failed: {title} @ {company} - {str(e)[:60]}"
                    })
                    await asyncio.sleep(0.3)

            await page.close()
            await context.close()

            # Refresh cookies after session
            if logged_in and cookies:
                try:
                    ctx2 = await browser.new_context()
                    await ctx2.add_cookies(cookies)
                    pg2 = await ctx2.new_page()
                    fresh_cookies = await ctx2.cookies()
                    save_cookies(platform_key, fresh_cookies)
                    await pg2.close()
                    await ctx2.close()
                except Exception:
                    pass

            await callback({
                "type": "platform_done", "platform": pname,
                "message": f"--- {pname} done: {len(platform_jobs)} jobs processed ---"
            })
            await asyncio.sleep(0.5)

        await browser.close()

    await callback({
        "type": "done", "applied": applied_count, "total": total,
        "message": f"Done! Applied to {applied_count}/{total} jobs today."
    })


async def _submit_application(page, platform, job, letter, profile):
    """Try to submit an application on the platform."""
    try:
        link = job.get("link", "")
        if not link:
            return False

        await page.goto(link, timeout=15000)
        await page.wait_for_timeout(2000)

        apply_btn = await page.query_selector(
            'button:has-text("Apply"), a:has-text("Apply"), '
            'button:has-text("apply"), a:has-text("apply")'
        )

        if apply_btn:
            await apply_btn.click()
            await page.wait_for_timeout(2000)

            textarea = await page.query_selector(
                'textarea[name*="cover"], textarea[name*="letter"], '
                'textarea[placeholder*="cover"], textarea[placeholder*="letter"], '
                'textarea[id*="cover"]'
            )
            if textarea:
                await textarea.fill(letter)

            submit = await page.query_selector(
                'button[type="submit"]:has-text("Submit"), '
                'button[type="submit"]:has-text("Apply"), '
                'input[type="submit"]'
            )
            if submit:
                await submit.click()
                await page.wait_for_timeout(3000)
                return True

    except Exception:
        pass

    return False
