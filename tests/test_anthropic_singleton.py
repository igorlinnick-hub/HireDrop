"""Anthropic client must be a per-process singleton.

Creating a new client on every cover-letter request creates new HTTP
connections, wastes latency, and is the kind of config/client/storage
smell CONVENTIONS.md calls out.
"""
import importlib

import modules.ai_cover_letter as mod


def setup_function(_func):
    mod._anthropic_client = None


def test_get_anthropic_client_returns_same_instance():
    a = mod.get_anthropic_client()
    b = mod.get_anthropic_client()
    assert a is b
    assert id(a) == id(b)


def test_singleton_survives_module_reimport_within_process():
    a = mod.get_anthropic_client()
    importlib.reload(mod)
    b = mod.get_anthropic_client()
    assert b is not None


def test_missing_api_key_raises_runtime_error(monkeypatch):
    monkeypatch.setattr(mod, "ANTHROPIC_API_KEY", "")
    mod._anthropic_client = None
    try:
        mod.get_anthropic_client()
    except RuntimeError as e:
        assert "ANTHROPIC_API_KEY" in str(e)
    else:
        raise AssertionError("expected RuntimeError")
