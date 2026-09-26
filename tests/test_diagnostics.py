"""Diagnostics must redact tokens/URLs and their key lists must match real entities.

Home Assistant is not installed for these tests. diagnostics_redact.py has no relative
imports, so it is run directly with a stand-in for
homeassistant.components.diagnostics.async_redact_data.
"""

from __future__ import annotations

import ast
import re
import runpy
import sys
import types
from pathlib import Path
from typing import Any
from unittest import TestCase, main

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/maverick_music_flow"


def _module(name: str, **attrs: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


def _real_async_redact_data(data: Any, to_redact: Any) -> Any:
    """A faithful stand-in for homeassistant.components.diagnostics.async_redact_data."""
    if isinstance(data, dict):
        redacted: dict[str, Any] = {}
        for key, value in data.items():
            if key in to_redact:
                redacted[key] = "**REDACTED**" if value else value
            else:
                redacted[key] = _real_async_redact_data(value, to_redact)
        return redacted
    if isinstance(data, list):
        return [_real_async_redact_data(item, to_redact) for item in data]
    return data


def load_diagnostics_redact() -> types.ModuleType:
    """Run diagnostics_redact.py with homeassistant.components.diagnostics stubbed."""
    stubs = {
        "homeassistant": _module("homeassistant"),
        "homeassistant.components": _module("homeassistant.components"),
        "homeassistant.components.diagnostics": _module(
            "homeassistant.components.diagnostics", async_redact_data=_real_async_redact_data
        ),
    }
    saved = {name: sys.modules.get(name) for name in stubs}
    installed = {name for name, module in saved.items() if module is None}
    sys.modules.update({name: module for name, module in stubs.items() if name in installed})
    try:
        namespace = runpy.run_path(str(COMPONENT / "diagnostics_redact.py"))
    finally:
        for name in installed:
            sys.modules.pop(name, None)
    return namespace


REDACT = load_diagnostics_redact()
redact_diagnostics = REDACT["redact_diagnostics"]


class RedactionTests(TestCase):
    def _payload(self) -> dict[str, Any]:
        return {
            "entry": {
                "music_assistant_token": "super-secret-token",
                "music_assistant_url": "http://ma.local:8095",
                "music_assistant_external_url": "https://ma.example.com:8095",
            },
            "context": {
                "music_assistant": {"authenticated": True, "schema_supported": True},
            },
            "required_connections": {
                "command_bridge": {
                    "last_error": (
                        "http://192.168.1.20:8095: Cannot connect to host 192.168.1.20:8095"
                    ),
                },
            },
            "stats": {
                "active_player": {
                    "entity_id": "media_player.kitchen",
                    "entity_picture": "/api/media_player_proxy/media_player.kitchen?token=abc123",
                    "media_image_url": "/api/media_player_proxy/media_player.kitchen?token=abc123",
                    "homeii_artwork_url": "/api/homeii_flow/artwork/xyz?auth_token=def456",
                    "artwork_candidates": [
                        "/api/media_player_proxy/media_player.kitchen?token=abc123",
                        "https://cdn.example.com/art.jpg?sig=extra-secret-suffix",
                    ],
                    "media_title": "Some Song",
                    "friendly_name": "Kitchen speaker",
                },
            },
            "some_custom_password_field": "hunter2",
        }

    def test_tokens_and_urls_are_gone(self) -> None:
        result = redact_diagnostics(self._payload())
        dumped = repr(result)
        for leaked in (
            "super-secret-token",
            "abc123",
            "def456",
            "extra-secret-suffix",
            "hunter2",
            "192.168.1.20",
        ):
            self.assertNotIn(leaked, dumped, f"{leaked!r} leaked into diagnostics: {dumped}")

    def test_urls_are_fully_redacted_not_just_query_stripped(self) -> None:
        result = redact_diagnostics(self._payload())
        self.assertEqual(result["entry"]["music_assistant_url"], "**REDACTED**")
        self.assertEqual(result["entry"]["music_assistant_external_url"], "**REDACTED**")
        self.assertEqual(result["entry"]["music_assistant_token"], "**REDACTED**")

    def test_artwork_candidates_and_artwork_urls_are_redacted(self) -> None:
        result = redact_diagnostics(self._payload())
        active_player = result["stats"]["active_player"]
        self.assertEqual(active_player["artwork_candidates"], "**REDACTED**")
        self.assertEqual(active_player["entity_picture"], "**REDACTED**")
        self.assertEqual(active_player["media_image_url"], "**REDACTED**")
        self.assertEqual(active_player["homeii_artwork_url"], "**REDACTED**")

    def test_custom_password_key_is_redacted(self) -> None:
        result = redact_diagnostics(self._payload())
        self.assertEqual(result["some_custom_password_field"], "**REDACTED**")

    def test_last_error_is_truncated_and_stripped_of_urls_and_ips(self) -> None:
        result = redact_diagnostics(self._payload())
        last_error = result["required_connections"]["command_bridge"]["last_error"]
        self.assertNotIn("192.168.1.20", last_error)
        self.assertNotIn("http://", last_error)
        self.assertLessEqual(len(last_error), 200)

    def test_non_sensitive_fields_survive(self) -> None:
        result = redact_diagnostics(self._payload())
        active_player = result["stats"]["active_player"]
        self.assertEqual(active_player["entity_id"], "media_player.kitchen")
        self.assertEqual(active_player["media_title"], "Some Song")
        self.assertEqual(active_player["friendly_name"], "Kitchen speaker")
        self.assertTrue(result["context"]["music_assistant"]["authenticated"])

    def test_query_strings_are_stripped_from_urls_left_unredacted(self) -> None:
        payload = {
            "frontend": {
                "brand_icon_url": "/maverick_music_flow/homeii-flow-icon.png?v=2",
            }
        }
        result = redact_diagnostics(payload)
        self.assertEqual(result["frontend"]["brand_icon_url"], "/maverick_music_flow/homeii-flow-icon.png")


def _string_list(tree: ast.AST, dict_key: str) -> list[str]:
    """Return the string literals of the list assigned to `dict_key` inside a dict literal."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == dict_key and isinstance(value, ast.List):
                return [element.value for element in value.elts if isinstance(element, ast.Constant)]
    raise AssertionError(f"{dict_key!r} not found")


DIAGNOSTICS_SOURCE = (COMPONENT / "diagnostics.py").read_text(encoding="utf-8")
DIAGNOSTICS_TREE = ast.parse(DIAGNOSTICS_SOURCE)


class KeyListTests(TestCase):
    def test_button_keys_match_real_button_entity_descriptions(self) -> None:
        button_source = (COMPONENT / "button.py").read_text(encoding="utf-8")
        real_keys = set(re.findall(r'key="([a-z_]+)"', button_source))
        listed_keys = set(_string_list(DIAGNOSTICS_TREE, "button_keys"))
        self.assertEqual(listed_keys, real_keys)

    def test_number_keys_match_real_unique_id_suffixes(self) -> None:
        number_source = (COMPONENT / "number.py").read_text(encoding="utf-8")
        # Static entity: f"{entry.entry_id}_system_screensaver_timeout"
        # Dynamic per-player entity family: f"{entry.entry_id}_volume_rule_max_{player}"
        self.assertIn("_system_screensaver_timeout", number_source)
        self.assertIn("_volume_rule_max_{player}", number_source)
        listed_keys = set(_string_list(DIAGNOSTICS_TREE, "number_keys"))
        self.assertEqual(listed_keys, {"volume_rule_max", "system_screensaver_timeout"})

    def test_button_keys_no_longer_list_the_stale_run_schedule_entry(self) -> None:
        listed_keys = _string_list(DIAGNOSTICS_TREE, "button_keys")
        self.assertNotIn("run_schedule", listed_keys)

    def test_button_keys_include_the_missing_screensaver_button(self) -> None:
        listed_keys = _string_list(DIAGNOSTICS_TREE, "button_keys")
        self.assertIn("show_system_screensaver_now", listed_keys)

    def test_sensor_and_binary_sensor_keys_still_match_descriptions(self) -> None:
        sensor_source = (COMPONENT / "sensor.py").read_text(encoding="utf-8")
        binary_sensor_source = (COMPONENT / "binary_sensor.py").read_text(encoding="utf-8")
        self.assertEqual(
            set(_string_list(DIAGNOSTICS_TREE, "sensor_keys")),
            set(re.findall(r'key="([a-z_]+)"', sensor_source)),
        )
        self.assertEqual(
            set(_string_list(DIAGNOSTICS_TREE, "binary_sensor_keys")),
            set(re.findall(r'key="([a-z_]+)"', binary_sensor_source)),
        )

    def test_diagnostics_source_calls_redact_diagnostics(self) -> None:
        self.assertIn("redact_diagnostics(", DIAGNOSTICS_SOURCE)

    def test_websocket_diagnostics_run_also_redacts(self) -> None:
        websocket_source = (COMPONENT / "websocket_api.py").read_text(encoding="utf-8")
        run_start = websocket_source.index("def websocket_run_diagnostics")
        run_end = websocket_source.index("\n@websocket_api.websocket_command", run_start + 1)
        self.assertIn("redact_diagnostics(", websocket_source[run_start:run_end])


if __name__ == "__main__":
    main()
