"""Per-user rate limit on cover letter generation.

Soft mode (RATE_LIMIT_ENFORCE=false) increments but never blocks — used
during soft-launch to gather data on who hits the limit. Hard mode blocks
with 429 once the daily quota is reached.
"""
import os
from unittest.mock import patch

import pytest


def test_under_limit_passes(auth_client):
    with patch("app.routers.tools.RATE_LIMIT_ENFORCE", True), \
         patch("app.routers.tools.RATE_LIMIT_LETTERS_PER_DAY", 3), \
         patch("app.routers.tools.usage_db.get_today_count", return_value=1), \
         patch("app.routers.tools.usage_db.increment_today", return_value=2), \
         patch("app.routers.tools.generate_cover_letter", return_value="hi"), \
         patch("app.routers.tools.get_profile", return_value={"name": "X"}):
        res = auth_client.post(
            "/api/v1/tools/cover-letter-preview",
            json={"keywords": "python"},
        )
    assert res.status_code == 200
    assert res.json()["letter"] == "hi"


def test_over_limit_returns_429(auth_client):
    with patch("app.routers.tools.RATE_LIMIT_ENFORCE", True), \
         patch("app.routers.tools.RATE_LIMIT_LETTERS_PER_DAY", 50), \
         patch("app.routers.tools.usage_db.get_today_count", return_value=50):
        res = auth_client.post(
            "/api/v1/tools/cover-letter-preview",
            json={"keywords": "python"},
        )
    assert res.status_code == 429
    assert "limit" in res.json()["detail"].lower()


def test_soft_mode_does_not_block(auth_client):
    with patch("app.routers.tools.RATE_LIMIT_ENFORCE", False), \
         patch("app.routers.tools.RATE_LIMIT_LETTERS_PER_DAY", 50), \
         patch("app.routers.tools.usage_db.get_today_count", return_value=1000), \
         patch("app.routers.tools.usage_db.increment_today", return_value=1001), \
         patch("app.routers.tools.generate_cover_letter", return_value="hi"), \
         patch("app.routers.tools.get_profile", return_value={"name": "X"}):
        res = auth_client.post(
            "/api/v1/tools/cover-letter-preview",
            json={"keywords": "python"},
        )
    assert res.status_code == 200
