"""Shared helpers for loading/saving profile and connections from JSON files.
These will be replaced by Supabase DB calls in Phase 2."""
import os
import json

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
PROFILE_PATH = os.path.join(DATA_DIR, "profile.json")
CONNECTIONS_PATH = os.path.join(DATA_DIR, "connections.json")

_PROFILE_DEFAULTS = {
    "name": "",
    "last_name": "",
    "email": "",
    "phone": "",
    "keywords": ["marketing", "content", "automation"],
    "location": "remote",
    "job_type": "full-time",
    "platforms": ["remoteok"],
    "writing_style": "",
    "platform_credentials": {},
}


def load_profile() -> dict:
    if os.path.exists(PROFILE_PATH):
        with open(PROFILE_PATH, "r") as f:
            return json.load(f)
    return dict(_PROFILE_DEFAULTS)


def save_profile(data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PROFILE_PATH, "w") as f:
        json.dump(data, f, indent=2)


def load_connections() -> dict:
    if os.path.exists(CONNECTIONS_PATH):
        with open(CONNECTIONS_PATH, "r") as f:
            return json.load(f)
    return {}


def save_connections(data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONNECTIONS_PATH, "w") as f:
        json.dump(data, f, indent=2)
