import asyncio
from playwright.async_api import async_playwright
from modules.ai_cover_letter import generate_cover_letter
from modules.encryption import decrypt_password
from database.db import update_job_status, save_application

DAILY_LIMIT = 50
PLATFORM_NAMES = {"remoteok": "RemoteOK", "indeed": "Indeed", "wellfound": "Wellfound"}


async def apply_to_jobs(jobs, profile, callback):
    """
    Apply to jobs using Playwright automation.
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
    platform_creds = profile.get("platform_credentials", {})

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        for platform_key, platform_jobs in platforms_order.items():
            pname = PLATFORM_NAMES.get(platform_key, platform_key.upper())
            creds = platform_creds.get(platform_key, {})

            await callback({
                "type": "platform_start", "platform": pname,
                "count": len(platform_jobs),
                "message": f"--- Starting {pname} ({len(platform_jobs)} jobs) ---"
            })
            await asyncio.sleep(0.5)

            # Try to login if credentials exist
            page = await browser.new_page()
            logged_in = False

            if creds.get("connected"):
                try:
                    logged_in = await _platform_login(
                        page, platform_key,
                        creds.get("email", ""),
                        decrypt_password(creds["password_enc"]) if creds.get("password_enc") else ""
                    )
                    if logged_in:
                        await callback({
                            "type": "generating", "platform": pname,
                            "message": f"[{pname}] Logged in successfully"
                        })
                    else:
                        await callback({
                            "type": "error", "platform": pname,
                            "message": f"[{pname}] Login failed - will generate letters only"
                        })
                except Exception as e:
                    await callback({
                        "type": "error", "platform": pname,
                        "message": f"[{pname}] Login error: {str(e)[:80]} - will generate letters only"
                    })

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

                    # Try to apply via Playwright if logged in
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
                    await asyncio.sleep(2.0)  # 2 second delay between applications

                except Exception as e:
                    await callback({
                        "type": "error", "platform": pname,
                        "job_id": job["id"],
                        "message": f"[{pname}] Failed: {title} @ {company} - {str(e)[:60]}"
                    })
                    await asyncio.sleep(0.3)

            await page.close()

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


async def _platform_login(page, platform, email, password):
    """Attempt to log into a platform. Returns True if successful."""
    if not email or not password:
        return False

    try:
        if platform == "remoteok":
            await page.goto("https://remoteok.com/login", timeout=10000)
            await page.fill('input[type="email"]', email)
            await page.fill('input[type="password"]', password)
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(3000)
            content = await page.content()
            return "logout" in content.lower() or "profile" in page.url

        elif platform == "indeed":
            await page.goto("https://secure.indeed.com/auth", timeout=10000)
            await page.fill('input[name="__email"]', email)
            await page.click('button[type="submit"]')
            await page.wait_for_selector('input[name="__password"]', timeout=5000)
            await page.fill('input[name="__password"]', password)
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(3000)
            return "indeed.com/account" in page.url or "dashboard" in page.url

        elif platform == "wellfound":
            await page.goto("https://wellfound.com/login", timeout=10000)
            await page.fill('input[name="user[email]"]', email)
            await page.fill('input[name="user[password]"]', password)
            await page.click('input[type="submit"]')
            await page.wait_for_timeout(3000)
            return "wellfound.com/jobs" in page.url or "dashboard" in page.url

    except Exception:
        return False

    return False


async def _submit_application(page, platform, job, letter, profile):
    """
    Try to submit an application on the platform.
    Returns True if submission was successful.
    This is platform-specific and may fail due to varying form structures.
    """
    try:
        link = job.get("link", "")
        if not link:
            return False

        await page.goto(link, timeout=15000)
        await page.wait_for_timeout(2000)

        # Look for common apply button patterns
        apply_btn = await page.query_selector(
            'button:has-text("Apply"), a:has-text("Apply"), '
            'button:has-text("apply"), a:has-text("apply")'
        )

        if apply_btn:
            await apply_btn.click()
            await page.wait_for_timeout(2000)

            # Try to fill cover letter textarea if visible
            textarea = await page.query_selector(
                'textarea[name*="cover"], textarea[name*="letter"], '
                'textarea[placeholder*="cover"], textarea[placeholder*="letter"], '
                'textarea[id*="cover"]'
            )
            if textarea:
                await textarea.fill(letter)

            # Try to submit
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
