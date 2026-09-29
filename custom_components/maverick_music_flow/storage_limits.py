"""Limits for user-controlled data kept in .storage (rewritten in full on every save)."""

from __future__ import annotations

import json
import re

DEFAULT_PROFILE = "default"
PROFILE_ID_PATTERN = re.compile(r"[a-z0-9_-]{1,32}")
MAX_PROFILES = 20
MAX_PLAYLISTS_PER_PROFILE = 100
MAX_URIS_PER_PLAYLIST = 500
MAX_URI_LENGTH = 1024
MAX_PLAYLIST_BYTES = 256 * 1024
MAX_WHEEL_CONTEXTS = 50
MAX_WHEEL_PREFERENCE_BYTES = 32 * 1024
MAX_INTERFACE_PREFERENCE_BYTES = 4 * 1024

_PROFILE_SECTIONS = ("saved_playlists", "interface_preferences", "wheel_preferences")


def known_profiles(storage):
    """Profiles that already hold stored data in any section."""
    return {
        profile
        for section in _PROFILE_SECTIONS
        for profile, data in storage.get(section, {}).items()
        if data
    }


def check_profile(storage, profile, is_admin):
    """Allow an existing profile; creating a new one needs a valid ID, an admin and room."""
    if profile == DEFAULT_PROFILE:
        return
    known = known_profiles(storage)
    if profile in known:
        return
    if not PROFILE_ID_PATTERN.fullmatch(profile):
        raise ValueError(
            "Invalid profile ID: use 1-32 lowercase letters, digits, underscores or hyphens"
        )
    if not is_admin:
        raise ValueError("Only an administrator can create a new profile")
    if len(known - {DEFAULT_PROFILE}) >= MAX_PROFILES:
        raise ValueError(f"Profile limit reached ({MAX_PROFILES})")


def json_size(value):
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
