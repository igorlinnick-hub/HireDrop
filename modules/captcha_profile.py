"""Single source of truth for per-platform captcha burden.

Why (PLAN_VOLUME_CAPTCHA_ECONOMICS.md §2): captcha cost differs per platform, and we
route users to LOW-touch (zero-captcha) destinations first. This map is consumed by:
  - modules/platforms/ats_boards.py  → ranks discovery (Greenhouse before Lever)
  - app/routers/jobs.py              → tags each job with captcha_touch/zero_touch so the
                                       dashboard can badge "quick apply" + the campaign can
                                       apply to zero-touch jobs first.

Measured/confirmed 2026-07-12/13:
  indeed       = invisible reCAPTCHA v3 (score-based)            → LOW  (zero-touch)
  greenhouse   = reCAPTCHA Enterprise invisible (no checkbox)    → LOW  (zero-touch, confirmed live)
  ziprecruiter = native; captcha profile not yet measured        → MEDIUM (assume some, pending measure)
  lever        = per-company hCaptcha (interactive image grid)   → HIGH (human-touch)
  ashby        = guest-apply, usually no interactive challenge    → LOW (zero-touch, pending live confirm)
Dependency-free on purpose so the DB/router layers can import it without pulling scrapers.
"""

PLATFORM_CAPTCHA = {
    "indeed": "low",
    "greenhouse": "low",
    "ziprecruiter": "medium",
    "lever": "high",
    "ashby": "low",
    # Workday: discovery-only (account-gated multi-step apply) → rank LAST in discovery so
    # zero-touch applyable boards fill the pool first; it still gets its per-platform share.
    "workday": "high",
}

# Runtime-learned per-company/platform overrides (token -> "low"|"medium"|"high").
# Empty until the extension's isDetected() feedback loop ships (see plan §2).
CAPTCHA_OVERRIDES: dict[str, str] = {}

TOUCH_RANK = {"low": 0, "medium": 1, "high": 2}


def captcha_touch(platform: str, token: str = "") -> str:
    """Expected human-solve burden for a platform (optionally refined per company token)."""
    if token and token in CAPTCHA_OVERRIDES:
        return CAPTCHA_OVERRIDES[token]
    return PLATFORM_CAPTCHA.get((platform or "").lower(), "medium")


def is_zero_touch(platform: str, token: str = "") -> bool:
    return captcha_touch(platform, token) == "low"
