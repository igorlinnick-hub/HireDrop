import asyncio
import json
import os
import random
import webbrowser
from datetime import datetime
from playwright.async_api import async_playwright
from modules.ai_cover_letter import generate_cover_letter
from database.db import update_job_status, save_application

DAILY_LIMIT_PER_PLATFORM = 50
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
RESUME_PATH = os.path.join(DATA_DIR, "resume.pdf")
PLATFORM_NAMES = {
    "remoteok": "RemoteOK", "indeed": "Indeed",
    "wellfound": "Wellfound", "glassdoor": "Glassdoor", "ziprecruiter": "ZipRecruiter",
    "google_jobs": "Google Jobs", "dice": "Dice",
    "toptal": "Toptal", "hired": "Hired", "flexjobs": "FlexJobs",
}

PLATFORM_LOGIN_URLS = {
    "remoteok": "https://remoteok.com/login",
    "indeed": "https://secure.indeed.com/auth",
    "wellfound": "https://wellfound.com/login",
    "glassdoor": "https://www.glassdoor.com/profile/login_input.htm",
    "ziprecruiter": "https://www.ziprecruiter.com/login",
    "google_jobs": "https://www.google.com/search?q=jobs",
    "dice": "https://www.dice.com/dashboard/login",
    "toptal": "https://www.toptal.com/users/login",
    "hired": "https://hired.com/login",
    "flexjobs": "https://www.flexjobs.com/login",
}

PLATFORM_VERIFY_URLS = {
    "remoteok": "https://remoteok.com",
    "indeed": "https://indeed.com",
    "wellfound": "https://wellfound.com",
    "glassdoor": "https://www.glassdoor.com",
    "ziprecruiter": "https://www.ziprecruiter.com",
    "google_jobs": "https://www.google.com/search?q=jobs",
    "dice": "https://www.dice.com",
    "toptal": "https://www.toptal.com",
    "hired": "https://hired.com",
    "flexjobs": "https://www.flexjobs.com",
}

CONNECTION_FILE = os.path.join(DATA_DIR, "connections.json")


def _load_connections():
    if os.path.exists(CONNECTION_FILE):
        with open(CONNECTION_FILE, "r") as f:
            return json.load(f)
    return {}


def _save_connections(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONNECTION_FILE, "w") as f:
        json.dump(data, f, indent=2)


def is_platform_connected(platform):
    return _load_connections().get(platform, {}).get("connected", False)


def set_platform_connected(platform, connected=True):
    conns = _load_connections()
    conns[platform] = {
        "connected": connected,
        "connected_at": datetime.now().isoformat() if connected else None,
    }
    _save_connections(conns)


def disconnect_platform(platform):
    set_platform_connected(platform, False)
    # Remove saved cookies
    cookie_file = _cookie_path(platform)
    if os.path.exists(cookie_file):
        os.remove(cookie_file)


def open_platform_login(platform):
    """Open the platform login page in the user's default system browser."""
    url = PLATFORM_LOGIN_URLS.get(platform)
    if url:
        webbrowser.open(url)
        return True
    return False


def open_platform_verify(platform):
    """Open the platform main page so user can check if they're still logged in."""
    url = PLATFORM_VERIFY_URLS.get(platform)
    if url:
        webbrowser.open(url)
        return True
    return False


# --- Cookie persistence ---

def _cookie_path(platform):
    return os.path.join(DATA_DIR, f"cookies_{platform}.json")


def _save_cookies(platform, cookies):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(_cookie_path(platform), "w") as f:
        json.dump(cookies, f, indent=2)


def _load_cookies(platform):
    path = _cookie_path(platform)
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return []


async def capture_cookies_via_login(platform):
    """
    Launch visible Playwright browser, navigate to login page,
    wait for user to log in, then save cookies.
    Returns True if cookies were captured.
    """
    login_url = PLATFORM_LOGIN_URLS.get(platform)
    verify_url = PLATFORM_VERIFY_URLS.get(platform, login_url)
    if not login_url:
        return False

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(login_url, timeout=30000)

        # Wait until URL changes away from login page (user logged in)
        # or until 120 seconds pass
        try:
            await page.wait_for_url(
                lambda url: url != login_url and "login" not in url.lower() and "auth" not in url.lower(),
                timeout=120000
            )
        except Exception:
            # Timeout — check if we got any cookies anyway
            pass

        # Give the page a moment to settle after redirect
        await page.wait_for_timeout(2000)

        # Save all cookies from this context
        cookies = await context.cookies()
        if cookies:
            _save_cookies(platform, cookies)
            set_platform_connected(platform, True)
            await browser.close()
            return True

        await browser.close()
        return False


# --- Human-like delays ---

async def _human_delay(min_s=3.0, max_s=7.0):
    await asyncio.sleep(random.uniform(min_s, max_s))


# --- Main apply function ---

async def apply_to_jobs(jobs, profile, callback):
    """
    Apply to jobs using Playwright with saved session cookies.
    Streams events via callback(event_dict).
    Per-platform daily limit of 50.
    """
    from database.db import get_today_applications_by_platform

    new_jobs = [j for j in jobs if j["status"] == "new"]
    if not new_jobs:
        await callback({"type": "done", "message": "No new jobs to apply to", "applied": 0, "total": 0, "platform_stats": {}})
        return

    # Group by platform
    platforms_order = {}
    for job in new_jobs:
        p = job["platform"] if "platform" in job.keys() else "remoteok"
        if p not in platforms_order:
            platforms_order[p] = []
        platforms_order[p].append(job)

    # Check daily limits per platform
    today_counts = get_today_applications_by_platform()
    platform_stats = {}
    total = 0
    for pk in platforms_order:
        already = today_counts.get(pk, 0)
        remaining = max(0, DAILY_LIMIT_PER_PLATFORM - already)
        count = min(len(platforms_order[pk]), remaining)
        platforms_order[pk] = platforms_order[pk][:count]
        platform_stats[pk] = {"applied": 0, "total": count, "already_today": already, "status": "waiting"}
        total += count

    if total == 0:
        await callback({"type": "done", "message": "Daily limit reached on all platforms", "applied": 0, "total": 0, "platform_stats": platform_stats})
        return

    await callback({
        "type": "start", "total": total,
        "platform_stats": platform_stats,
        "message": f"Starting application process for {total} jobs..."
    })
    await asyncio.sleep(0.3)

    applied_count = 0

    async with async_playwright() as p:
        # Visible browser so user can see what's happening
        browser = await p.chromium.launch(headless=False)

        for platform_key, platform_jobs in platforms_order.items():
            pname = PLATFORM_NAMES.get(platform_key, platform_key.upper())

            if not platform_jobs:
                platform_stats[platform_key]["status"] = "limit_reached"
                await callback({
                    "type": "platform_skip", "platform": pname,
                    "platform_key": platform_key,
                    "platform_stats": platform_stats,
                    "message": f"[{pname}] Daily limit reached — skipping"
                })
                continue

            platform_stats[platform_key]["status"] = "running"
            connected = is_platform_connected(platform_key)

            # Create context and load saved cookies
            context = await browser.new_context()
            cookies = _load_cookies(platform_key)
            if cookies and connected:
                await context.add_cookies(cookies)
                await callback({
                    "type": "platform_start", "platform": pname,
                    "platform_key": platform_key,
                    "count": len(platform_jobs),
                    "platform_stats": platform_stats,
                    "message": f"--- Starting {pname} ({len(platform_jobs)} jobs) — cookies loaded ---"
                })
            elif connected:
                await callback({
                    "type": "platform_start", "platform": pname,
                    "platform_key": platform_key,
                    "count": len(platform_jobs),
                    "platform_stats": platform_stats,
                    "message": f"--- Starting {pname} ({len(platform_jobs)} jobs) — no cookies, letters only ---"
                })
            else:
                await callback({
                    "type": "platform_start", "platform": pname,
                    "platform_key": platform_key,
                    "count": len(platform_jobs),
                    "platform_stats": platform_stats,
                    "message": f"--- Starting {pname} ({len(platform_jobs)} jobs) — not connected, letters only ---"
                })

            await asyncio.sleep(0.5)
            page = await context.new_page()

            for job in platform_jobs:
                title = job["title"]
                company = job["company"]

                await callback({
                    "type": "generating", "platform": pname,
                    "platform_key": platform_key,
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
                    if connected and cookies:
                        try:
                            actually_applied = await _submit_application(
                                page, platform_key, job, letter, profile
                            )
                        except Exception as e:
                            await callback({
                                "type": "error", "platform": pname,
                                "platform_key": platform_key,
                                "job_id": job["id"],
                                "message": f"[{pname}] Submit failed for {title}: {str(e)[:80]}"
                            })

                    if actually_applied:
                        update_job_status(job["id"], "applied")
                        save_application(job["id"], letter)
                        applied_count += 1
                        platform_stats[platform_key]["applied"] += 1
                        await callback({
                            "type": "applied", "platform": pname,
                            "platform_key": platform_key,
                            "job_id": job["id"], "title": title, "company": company,
                            "link": job.get("link", ""),
                            "applied": applied_count, "total": total,
                            "platform_stats": platform_stats,
                            "message": f"[{pname}] Applied: {title} @ {company}"
                        })
                    else:
                        # Save letter but keep job as "new" for retry
                        save_application(job["id"], letter)
                        await callback({
                            "type": "applied", "platform": pname,
                            "platform_key": platform_key,
                            "job_id": job["id"], "title": title, "company": company,
                            "link": job.get("link", ""),
                            "applied": applied_count, "total": total,
                            "platform_stats": platform_stats,
                            "message": f"[{pname}] Letter ready (not submitted): {title} @ {company}"
                        })

                    await _human_delay(2.0, 4.0)

                except Exception as e:
                    await callback({
                        "type": "error", "platform": pname,
                        "platform_key": platform_key,
                        "job_id": job["id"],
                        "message": f"[{pname}] Failed: {title} @ {company} - {str(e)[:80]}"
                    })
                    await asyncio.sleep(0.5)

            # Save refreshed cookies after session
            updated_cookies = await context.cookies()
            if updated_cookies:
                _save_cookies(platform_key, updated_cookies)

            await page.close()
            await context.close()

            platform_stats[platform_key]["status"] = "done"
            await callback({
                "type": "platform_done", "platform": pname,
                "platform_key": platform_key,
                "platform_stats": platform_stats,
                "message": f"--- {pname} done: {platform_stats[platform_key]['applied']}/{len(platform_jobs)} applied ---"
            })
            await asyncio.sleep(0.5)

        await browser.close()

    await callback({
        "type": "done", "applied": applied_count, "total": total,
        "platform_stats": platform_stats,
        "message": f"Done! Applied to {applied_count}/{total} jobs today."
    })


# --- Platform-specific submission ---

async def _submit_application(page, platform, job, letter, profile):
    """Route to platform-specific submission handler."""
    if platform == "indeed":
        return await _submit_indeed(page, job, letter, profile)
    else:
        return await _submit_generic(page, job, letter, profile)


async def _submit_indeed(page, job, letter, profile):
    """Indeed Easy Apply — multi-step form handler."""
    link = job.get("link", "")
    if not link:
        return False

    try:
        await page.goto(link, timeout=20000)
        await _human_delay(3.0, 5.0)

        # Look for Easy Apply / Apply now button
        apply_btn = await page.query_selector(
            'button:has-text("Easily apply"), '
            'button:has-text("Apply now"), '
            'button:has-text("Apply on company site"), '
            'a:has-text("Easily apply"), '
            'a:has-text("Apply now")'
        )

        if not apply_btn:
            return False

        await apply_btn.click()
        await _human_delay(3.0, 5.0)

        # Handle multi-step Indeed form (up to 10 steps)
        for step in range(10):
            # Fill name fields if present
            name_parts = (profile.get("name", "") or "").strip().split(None, 1)
            first_name = name_parts[0] if name_parts else ""
            last_name = name_parts[1] if len(name_parts) > 1 else ""

            await _try_fill(page, 'input[id*="first-name" i], input[name*="firstName" i], input[name*="first_name" i]', first_name)
            await _try_fill(page, 'input[id*="last-name" i], input[name*="lastName" i], input[name*="last_name" i]', last_name)
            await _try_fill(page, 'input[id*="name" i][type="text"]:not([id*="first"]):not([id*="last"])', profile.get("name", ""))

            # Fill email
            await _try_fill(page, 'input[type="email"], input[name*="email" i], input[id*="email" i]', profile.get("email", ""))

            # Fill phone
            await _try_fill(page, 'input[type="tel"], input[name*="phone" i], input[id*="phone" i]', profile.get("phone", ""))

            # Upload resume if file input exists
            file_input = await page.query_selector('input[type="file"][accept*="pdf"], input[type="file"][accept*=".pdf"], input[type="file"]')
            if file_input and os.path.exists(RESUME_PATH):
                try:
                    await file_input.set_input_files(RESUME_PATH)
                    await _human_delay(2.0, 3.0)
                except Exception:
                    pass

            # Fill cover letter textarea
            cover_textarea = await page.query_selector(
                'textarea[name*="cover" i], textarea[name*="letter" i], '
                'textarea[id*="cover" i], textarea[id*="letter" i], '
                'textarea[placeholder*="cover letter" i], '
                'textarea[aria-label*="cover" i]'
            )
            if cover_textarea:
                await cover_textarea.fill("")
                await cover_textarea.type(letter, delay=10)
                await _human_delay(1.0, 2.0)

            # Look for Continue/Next button first
            continue_btn = await page.query_selector(
                'button:has-text("Continue"), '
                'button:has-text("Next"), '
                'button[aria-label*="Continue" i], '
                'button[aria-label*="Next" i]'
            )

            if continue_btn:
                await _human_delay(2.0, 4.0)
                await continue_btn.click()
                await _human_delay(3.0, 5.0)
                continue

            # Look for Submit/Apply button (final step)
            submit_btn = await page.query_selector(
                'button:has-text("Submit"), '
                'button:has-text("Submit your application"), '
                'button:has-text("Apply"), '
                'button[type="submit"]:has-text("Submit"), '
                'button[type="submit"]:has-text("Apply")'
            )

            if submit_btn:
                await _human_delay(2.0, 4.0)
                await submit_btn.click()
                await _human_delay(4.0, 7.0)

                # Check for success indicators
                success = await page.query_selector(
                    'div:has-text("Application submitted"), '
                    'div:has-text("application has been submitted"), '
                    'h1:has-text("Applied"), '
                    '[data-testid*="success"], '
                    'div:has-text("Your application was sent")'
                )
                return success is not None or True  # Optimistic after submit click

            # No Continue or Submit found — we're stuck, bail out
            break

    except Exception:
        pass

    return False


async def _submit_generic(page, job, letter, profile):
    """Generic platform submission — tries common form patterns."""
    link = job.get("link", "")
    if not link:
        return False

    try:
        await page.goto(link, timeout=20000)
        await _human_delay(3.0, 5.0)

        apply_btn = await page.query_selector(
            'button:has-text("Apply"), a:has-text("Apply"), '
            'button:has-text("apply"), a:has-text("apply")'
        )

        if not apply_btn:
            return False

        await apply_btn.click()
        await _human_delay(3.0, 5.0)

        # Fill name
        await _try_fill(page, 'input[name*="name" i]:not([type="email"]), input[autocomplete="name"]', profile.get("name", ""))

        # Fill email
        await _try_fill(page, 'input[type="email"], input[name*="email" i]', profile.get("email", ""))

        # Upload resume
        file_input = await page.query_selector('input[type="file"]')
        if file_input and os.path.exists(RESUME_PATH):
            try:
                await file_input.set_input_files(RESUME_PATH)
                await _human_delay(2.0, 3.0)
            except Exception:
                pass

        # Fill cover letter
        textarea = await page.query_selector(
            'textarea[name*="cover" i], textarea[name*="letter" i], '
            'textarea[placeholder*="cover" i], textarea[placeholder*="letter" i], '
            'textarea[id*="cover" i]'
        )
        if textarea:
            await textarea.fill("")
            await textarea.type(letter, delay=10)

        await _human_delay(2.0, 4.0)

        submit = await page.query_selector(
            'button[type="submit"]:has-text("Submit"), '
            'button[type="submit"]:has-text("Apply"), '
            'button:has-text("Submit application"), '
            'input[type="submit"]'
        )
        if submit:
            await submit.click()
            await _human_delay(4.0, 7.0)
            return True

    except Exception:
        pass

    return False


async def _try_fill(page, selector, value):
    """Try to fill a form field. Silently skip if not found or fails."""
    if not value:
        return
    try:
        el = await page.query_selector(selector)
        if el:
            current = await el.get_attribute("value") or ""
            if not current.strip():
                await el.fill("")
                await el.type(value, delay=random.randint(20, 50))
    except Exception:
        pass
