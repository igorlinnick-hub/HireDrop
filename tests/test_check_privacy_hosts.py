"""scripts/check_privacy_hosts.py — the manifest/policy comparison, without the network."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "check_privacy_hosts", ROOT / "scripts" / "check_privacy_hosts.py"
)
cph = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cph)

MANIFEST = json.loads((ROOT / "chrome-extension" / "manifest.json").read_text())

# The 10-05 policy sentence that prompted the check.
OLD = "<p>The HireDrop Chrome extension runs only on indeed.com and on the HireDrop dashboard domain.</p>"
NEW = (
    "<p>Runs on indeed.com, ziprecruiter.com, greenhouse.io, jobs.lever.co and ashbyhq.com "
    "&mdash; and on hiredrop.io. The HireDrop backend and Supabase store data. "
    "The optional pill shows on other websites you visit.</p>"
)


def test_required_phrase():
    assert cph.required_phrase("https://*.indeed.com/*") == "indeed.com"
    assert cph.required_phrase("https://job-boards.eu.greenhouse.io/*") == "greenhouse.io"
    assert cph.required_phrase("https://jobs.lever.co/*") == "lever.co"
    assert cph.required_phrase("https://web-production-db45.up.railway.app/*") == "HireDrop backend"
    assert cph.required_phrase("<all_urls>") == "other websites"


def test_old_policy_fails_on_the_real_manifest():
    gaps = {phrase for _, phrase in cph.missing(MANIFEST, cph.page_text(OLD))}
    assert {
        "ziprecruiter.com",
        "greenhouse.io",
        "lever.co",
        "ashbyhq.com",
        "other websites",
    } <= gaps


def test_complete_policy_passes():
    assert cph.missing(MANIFEST, cph.page_text(NEW)) == []


def test_new_platform_in_manifest_fails_until_named():
    m = json.loads(json.dumps(MANIFEST))
    m["host_permissions"].append("https://*.myworkdayjobs.com/*")
    assert cph.missing(m, cph.page_text(NEW)) == [
        ("https://*.myworkdayjobs.com/*", "myworkdayjobs.com")
    ]


def test_cli_exit_codes(tmp_path):
    good, bad = tmp_path / "good.html", tmp_path / "bad.html"
    good.write_text(NEW)
    bad.write_text(OLD)
    assert cph.main(["--file", str(good)]) == 0
    assert cph.main(["--file", str(bad)]) == 1
    assert cph.main(["--file", str(tmp_path / "nope.html")]) == 2
