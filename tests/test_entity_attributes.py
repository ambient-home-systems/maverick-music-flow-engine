"""Sensor attributes stay stable so the recorder does not store a row per update (B-7b)."""

from __future__ import annotations

import runpy
from pathlib import Path
from unittest import TestCase, main

MODULE = runpy.run_path(
    str(
        Path(__file__).resolve().parents[1]
        / "custom_components/maverick_music_flow/entity_attributes.py"
    )
)
stable_attributes = MODULE["stable_attributes"]
status_attributes = MODULE["status_attributes"]
UNRECORDED_ATTRIBUTES = MODULE["UNRECORDED_ATTRIBUTES"]


class StableAttributesTests(TestCase):
    def test_per_call_values_are_dropped_at_every_level(self):
        value = {
            "status": "healthy",
            "generated_at": "2026-09-29T12:00:00+00:00",
            "last_updated_at": "2026-09-29T12:00:00+00:00",
            "realtime_events": {"ok": True, "event_count": 812, "last_event_at": "12:00"},
            "screensaver": {"mode": "clock", "generated_at": "2026-09-29T12:00:00+00:00"},
            "players_today": [{"entity_id": "media_player.kitchen", "generated_at": "x"}],
        }
        self.assertEqual(
            stable_attributes(value),
            {
                "status": "healthy",
                "realtime_events": {"ok": True},
                "screensaver": {"mode": "clock"},
                "players_today": [{"entity_id": "media_player.kitchen"}],
            },
        )

    def test_the_input_is_not_changed(self):
        # required_connections_snapshot() is shared by every entity for one update.
        shared = {"generated_at": "now", "music_assistant": {"ok": True, "event_count": 3}}
        stable_attributes(shared)
        self.assertEqual(
            shared, {"generated_at": "now", "music_assistant": {"ok": True, "event_count": 3}}
        )

    def test_status_attributes_drop_the_capability_map(self):
        context = {"available": True, "version": "1.0.0", "capabilities": {"stats": True}}
        self.assertEqual(status_attributes(context), {"available": True, "version": "1.0.0"})
        self.assertIn("capabilities", context)

    def test_large_and_changing_attributes_are_not_recorded(self):
        for key in ("required_connections", "media_cache", "connections", "players", "probe"):
            self.assertIn(key, UNRECORDED_ATTRIBUTES)
        for key in ("available", "version", "status", "ok", "message", "summary"):
            self.assertNotIn(key, UNRECORDED_ATTRIBUTES)


if __name__ == "__main__":
    main()
