"""CSP report intake: both wire formats, our pages only, one log line per
violation per hour, extension noise and oversize bodies dropped, always 204."""

import json

import pytest

from app.routers import csp

URL = "/api/v1/csp-report"


@pytest.fixture(autouse=True)
def _fresh_memo():
    csp._seen.clear()
    csp._attempts.clear()
    yield
    csp._seen.clear()
    csp._attempts.clear()


def _legacy(blocked, doc="https://hiredrop.io/dashboard?code=secret", directive="script-src-elem"):
    return json.dumps(
        {
            "csp-report": {
                "document-uri": doc,
                "blocked-uri": blocked,
                "effective-directive": directive,
            }
        }
    )


def _post(client, body, ctype="application/csp-report"):
    return client.post(URL, content=body, headers={"content-type": ctype})


def test_source_file_is_logged_as_origin(client, capsys):
    body = json.dumps(
        {
            "csp-report": {
                "document-uri": "https://hiredrop.io/dashboard/settings",
                "blocked-uri": "data",
                "effective-directive": "connect-src",
                "source-file": "https://hiredrop.io/_next/static/chunks/abc.js?v=1",
            }
        }
    )
    _post(client, body)
    err = capsys.readouterr().err
    assert "blocked=data doc=/dashboard/settings src=https://hiredrop.io" in err


def test_extension_script_on_our_page_is_dropped(client, capsys):
    body = [
        {
            "type": "csp-violation",
            "body": {
                "documentURL": "https://hiredrop.io/dashboard/settings",
                "blockedURL": "data",
                "effectiveDirective": "connect-src",
                "sourceFile": "chrome-extension://kbfnbcaeplbcioakkpcpgfkobkghlhen/inject.js",
            },
        }
    ]
    _post(client, json.dumps(body), "application/reports+json")
    assert "[csp]" not in capsys.readouterr().err


def test_legacy_report_logs_origin_and_path_only(client, capsys):
    r = _post(client, _legacy("https://evil.example/x.js?token=abc"))
    assert r.status_code == 204
    err = capsys.readouterr().err
    assert "[csp] directive=script-src-elem blocked=https://evil.example doc=/dashboard" in err
    assert "token=abc" not in err and "secret" not in err


def test_reporting_api_batch(client, capsys):
    body = json.dumps(
        [
            {
                "type": "csp-violation",
                "body": {
                    "documentURL": "https://www.hiredrop.io/login",
                    "blockedURL": "inline",
                    "effectiveDirective": "style-src-attr",
                },
            },
            {"type": "deprecation", "body": {"documentURL": "https://hiredrop.io/"}},
        ]
    )
    assert _post(client, body, "application/reports+json").status_code == 204
    err = capsys.readouterr().err
    assert "[csp] directive=style-src-attr blocked=inline doc=/login" in err
    assert err.count("[csp]") == 1


def test_same_violation_logged_once_then_counted(client, capsys):
    for _ in range(5):
        _post(client, _legacy("https://x.example/a.js"))
    assert capsys.readouterr().err.count("[csp]") == 1
    first, swallowed = csp._seen["script-src-elem https://x.example /dashboard "]
    assert swallowed == 4
    # An hour later the next report logs again, carrying the count.
    csp._seen["script-src-elem https://x.example /dashboard "] = (first - 3601, swallowed)
    _post(client, _legacy("https://x.example/a.js"))
    assert "(+4 in the last hour)" in capsys.readouterr().err


@pytest.mark.parametrize(
    "body",
    [
        _legacy("chrome-extension://abcdef/inject.js"),
        _legacy("https://x.example/a.js", doc="https://other-site.com/page"),
        "not json",
        json.dumps({"something": "else"}),
        "x" * (17 * 1024),
    ],
)
def test_noise_is_dropped_silently(client, capsys, body):
    assert _post(client, body).status_code == 204
    assert "[csp]" not in capsys.readouterr().err


def test_vercel_preview_counts_as_ours(client, capsys):
    _post(
        client, _legacy("https://x.example/a.js", doc="https://jobflow-website-git-x.vercel.app/")
    )
    assert "[csp]" in capsys.readouterr().err


def test_per_ip_ceiling(client, capsys, monkeypatch):
    monkeypatch.setattr(csp, "_MAX_PER_IP", 2)
    for i in range(4):
        _post(client, _legacy(f"https://x{i}.example/a.js"))
    assert capsys.readouterr().err.count("[csp]") == 2
