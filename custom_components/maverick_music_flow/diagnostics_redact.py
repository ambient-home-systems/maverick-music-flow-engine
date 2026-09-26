"""Shared redaction for diagnostics output.

Used by both the diagnostics.py config-entry download and the
maverick_music_flow/diagnostics/run WebSocket command, so a user pasting either
into a GitHub issue does not leak Music Assistant tokens or working artwork
links (which serve images without authentication).
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components.diagnostics import async_redact_data

TO_REDACT = {
    "music_assistant_token",
    "token",
    "access_token",
    "music_assistant_url",
    "music_assistant_external_url",
    "ma_url",
    "url",
    "entity_picture",
    "media_image_url",
    "artwork_candidates",
    "homeii_artwork_url",
    "image",
    "image_url",
}

_SENSITIVE_KEY_SUBSTRINGS = ("token", "password")

_LAST_ERROR_MAX_LEN = 200
_URL_RE = re.compile(r"https?://\S+")
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_QUERY_STRING_RE = re.compile(r"\?[^\s\"'<>]*")


def _is_sensitive_key(key: Any) -> bool:
    """Return whether a key name should always be redacted."""
    key_str = str(key).lower()
    return any(substring in key_str for substring in _SENSITIVE_KEY_SUBSTRINGS)


def _collect_sensitive_keys(value: Any, found: set[str]) -> None:
    """Recursively collect key names containing 'token' or 'password'."""
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_sensitive_key(key):
                found.add(str(key))
            _collect_sensitive_keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _collect_sensitive_keys(item, found)


def _sanitize_last_error(value: str) -> str:
    """Truncate a last_error string and remove URLs and IP addresses from it."""
    sanitized = _URL_RE.sub("[redacted-url]", value)
    sanitized = _IP_RE.sub("[redacted-ip]", sanitized)
    return sanitized[:_LAST_ERROR_MAX_LEN]


def _strip_query_string(value: str) -> str:
    """Drop the query string from a URL-like value, keeping the path."""
    return _QUERY_STRING_RE.sub("", value)


def _scrub(value: Any, *, parent_key: str | None = None) -> Any:
    """Strip query strings from remaining URLs and sanitize last_error strings."""
    if isinstance(value, dict):
        return {key: _scrub(item, parent_key=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item, parent_key=parent_key) for item in value]
    if isinstance(value, str):
        if parent_key == "last_error":
            return _sanitize_last_error(value)
        return _strip_query_string(value)
    return value


def redact_diagnostics(data: dict[str, Any]) -> dict[str, Any]:
    """Redact tokens, URLs, artwork links and error strings from diagnostics data."""
    sensitive_keys: set[str] = set(TO_REDACT)
    _collect_sensitive_keys(data, sensitive_keys)
    redacted = async_redact_data(data, sensitive_keys)
    return _scrub(redacted)
