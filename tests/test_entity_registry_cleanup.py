"""Regression tests for finding B-1: switch.py's stale-registry cleanup used to delete
the "Max volume" number entities because it matched registry entries by unique_id prefix
only, without checking the entity's platform (domain).

Home Assistant is not installed for these tests. switch.py and number.py are imported
with small stand-ins for the Home Assistant modules they use (the same technique as
test_authorization.py), so the real setup/cleanup functions run against a fake entity
registry and a fake runtime.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import TestCase, main

ROOT_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components/maverick_music_flow"
PACKAGE = "_mmf_registry_cleanup_under_test"


def _module(name: str, **attrs: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


class FakeRegistryEntry(SimpleNamespace):
    """Stand-in for homeassistant.helpers.entity_registry.RegistryEntry."""


class FakeEntityRegistry:
    """Stand-in for the Home Assistant entity registry, shared across platforms."""

    def __init__(self) -> None:
        self.entities: dict[str, FakeRegistryEntry] = {}

    def add(self, entity_id: str, unique_id: str, config_entry_id: str) -> None:
        self.entities[entity_id] = FakeRegistryEntry(
            entity_id=entity_id, unique_id=unique_id, config_entry_id=config_entry_id
        )

    def async_get(self, entity_id: str) -> FakeRegistryEntry | None:
        return self.entities.get(entity_id)

    def async_get_entity_id(self, domain: str, platform: str, unique_id: str) -> str | None:
        for registry_entry in self.entities.values():
            if registry_entry.entity_id.split(".", 1)[0] != domain:
                continue
            if registry_entry.unique_id == unique_id:
                return registry_entry.entity_id
        return None

    def async_remove(self, entity_id: str) -> None:
        self.entities.pop(entity_id, None)


DISPATCHED: list[tuple[Any, Any, Any]] = []


def _async_dispatcher_connect(hass: Any, signal: Any, target: Any):
    DISPATCHED.append((hass, signal, target))
    return lambda: None


_HA_STUBS = {
    "homeassistant": _module("homeassistant"),
    "homeassistant.components": _module("homeassistant.components"),
    "homeassistant.components.switch": _module(
        "homeassistant.components.switch", SwitchEntity=type("SwitchEntity", (), {"entity_id": None})
    ),
    "homeassistant.components.number": _module(
        "homeassistant.components.number",
        NumberEntity=type("NumberEntity", (), {"entity_id": None}),
        NumberMode=SimpleNamespace(SLIDER="slider", BOX="box"),
    ),
    "homeassistant.config_entries": _module("homeassistant.config_entries", ConfigEntry=object),
    "homeassistant.core": _module(
        "homeassistant.core", HomeAssistant=object, callback=lambda func: func
    ),
    "homeassistant.helpers": _module("homeassistant.helpers"),
    "homeassistant.helpers.dispatcher": _module(
        "homeassistant.helpers.dispatcher", async_dispatcher_connect=_async_dispatcher_connect
    ),
    "homeassistant.helpers.entity": _module("homeassistant.helpers.entity", DeviceInfo=dict),
    "homeassistant.helpers.entity_platform": _module(
        "homeassistant.helpers.entity_platform", AddEntitiesCallback=object
    ),
    "homeassistant.helpers.event": _module(
        "homeassistant.helpers.event", async_track_point_in_time=lambda *args, **kwargs: (lambda: None)
    ),
}


def _load(registry: FakeEntityRegistry):
    """Import switch.py and number.py without Home Assistant, bound to one fake registry.

    Each test gets its own fake registry, so the modules are re-imported fresh every
    time rather than reused from sys.modules (which would keep the previous test's
    registry bound via the already-executed `from ... import async_get as ...` alias).
    """
    for name in list(sys.modules):
        if name == PACKAGE or name.startswith(f"{PACKAGE}."):
            del sys.modules[name]
    stub = types.ModuleType(PACKAGE)
    stub.__path__ = [str(ROOT_COMPONENT)]
    stub.async_get_runtime = lambda hass: hass.runtime
    sys.modules[PACKAGE] = stub
    sys.modules[f"{PACKAGE}.runtime"] = _module(
        f"{PACKAGE}.runtime",
        HomeiiFlowRuntime=object,
        _due_schedule_datetime=lambda *args, **kwargs: None,
        _homeii_weekday=lambda *args, **kwargs: 0,
        _local_datetime=lambda *args, **kwargs: None,
        _next_schedule_datetime=lambda *args, **kwargs: None,
        _parse_utc_datetime=lambda *args, **kwargs: None,
        _utc_iso=lambda: "now",
    )
    _HA_STUBS["homeassistant.helpers.entity_registry"] = _module(
        "homeassistant.helpers.entity_registry", async_get=lambda hass: registry
    )
    saved = {name: sys.modules.get(name) for name in _HA_STUBS}
    installed = {name for name, module in saved.items() if module is None}
    sys.modules.update({name: module for name, module in _HA_STUBS.items() if name in installed})
    try:
        switch = importlib.import_module(f"{PACKAGE}.switch")
        number = importlib.import_module(f"{PACKAGE}.number")
    finally:
        for name in installed:
            sys.modules.pop(name, None)
    return switch, number


class FakeRuntime:
    """Minimal runtime stand-in exposing only what the platforms read."""

    def __init__(self) -> None:
        self._schedules: list[dict[str, Any]] = []
        self._timers: list[dict[str, Any]] = []
        self._volume_rules: list[dict[str, Any]] = []

    def schedules(self, profile_id: str | None = None) -> list[dict[str, Any]]:
        return list(self._schedules)

    def timers(self, profile_id: str | None = None) -> list[dict[str, Any]]:
        return list(self._timers)

    def volume_rules(self, profile_id: str | None = None) -> list[dict[str, Any]]:
        return list(self._volume_rules)


class RegistryCleanupTests(TestCase):
    def setUp(self) -> None:
        DISPATCHED.clear()
        self.registry = FakeEntityRegistry()
        self.switch, self.number = _load(self.registry)
        self.runtime = FakeRuntime()
        self.entry = SimpleNamespace(
            entry_id="entry1",
            data={},
            options={},
            title="Test",
            async_on_unload=lambda _fn: None,
        )
        self.hass = SimpleNamespace(runtime=self.runtime, states=SimpleNamespace(get=lambda _id: None))

    def _setup(self, module):
        """Run a platform's async_setup_entry and return (added_entities, fire_signal)."""
        added: list[Any] = []

        def async_add_entities(entities):
            added.extend(entities)

        before = len(DISPATCHED)
        asyncio.run(module.async_setup_entry(self.hass, self.entry, async_add_entities))
        target = DISPATCHED[before][2]

        def fire_signal() -> None:
            target()

        return added, fire_signal

    def test_volume_rule_switch_and_number_both_survive_repeated_engine_updates(self) -> None:
        player = "media_player.x"
        self.runtime._volume_rules = [{"player": player, "max_volume": 40, "enabled": True}]

        switch_added, fire_switch_signal = self._setup(self.switch)
        number_added, fire_number_signal = self._setup(self.number)

        switch_entity_id = f"switch.max_volume_{player}"
        number_entity_id = f"number.max_volume_{player}"
        switch_rule_entity = next(e for e in switch_added if getattr(e, "_player", None) == player)
        number_rule_entity = next(e for e in number_added if getattr(e, "_player", None) == player)
        self.registry.add(switch_entity_id, switch_rule_entity._attr_unique_id, "entry1")
        self.registry.add(number_entity_id, number_rule_entity._attr_unique_id, "entry1")

        # Simulate several SIGNAL_ENGINE_UPDATED dispatches (e.g. every 30 seconds).
        for _ in range(5):
            fire_switch_signal()
            fire_number_signal()

        self.assertIn(switch_entity_id, self.registry.entities)
        self.assertIn(number_entity_id, self.registry.entities)

    def test_deleting_the_rule_removes_both_registry_entries(self) -> None:
        player = "media_player.x"
        self.runtime._volume_rules = [{"player": player, "max_volume": 40, "enabled": True}]

        switch_added, fire_switch_signal = self._setup(self.switch)
        number_added, fire_number_signal = self._setup(self.number)
        switch_entity_id = f"switch.max_volume_{player}"
        number_entity_id = f"number.max_volume_{player}"
        switch_rule_entity = next(e for e in switch_added if getattr(e, "_player", None) == player)
        number_rule_entity = next(e for e in number_added if getattr(e, "_player", None) == player)
        self.registry.add(switch_entity_id, switch_rule_entity._attr_unique_id, "entry1")
        self.registry.add(number_entity_id, number_rule_entity._attr_unique_id, "entry1")

        self.runtime._volume_rules = []
        fire_switch_signal()
        fire_number_signal()

        self.assertNotIn(switch_entity_id, self.registry.entities)
        self.assertNotIn(number_entity_id, self.registry.entities)

    def test_schedule_and_timer_switch_cleanup_still_works(self) -> None:
        self.runtime._schedules = [{"id": "keep", "enabled": True}]
        self.runtime._timers = [{"id": "keep-timer", "enabled": True}]

        switch_added, fire_switch_signal = self._setup(self.switch)
        schedule_entity = next(e for e in switch_added if getattr(e, "_schedule_id", None) == "keep")
        timer_entity = next(e for e in switch_added if getattr(e, "_timer_id", None) == "keep-timer")

        stale_schedule_entity_id = "switch.old_schedule"
        stale_timer_entity_id = "switch.old_timer"
        live_schedule_entity_id = "switch.live_schedule"
        live_timer_entity_id = "switch.live_timer"
        self.registry.add(stale_schedule_entity_id, "entry1_schedule_gone", "entry1")
        self.registry.add(stale_timer_entity_id, "entry1_timer_gone", "entry1")
        self.registry.add(live_schedule_entity_id, schedule_entity._attr_unique_id, "entry1")
        self.registry.add(live_timer_entity_id, timer_entity._attr_unique_id, "entry1")

        fire_switch_signal()

        self.assertNotIn(stale_schedule_entity_id, self.registry.entities)
        self.assertNotIn(stale_timer_entity_id, self.registry.entities)
        self.assertIn(live_schedule_entity_id, self.registry.entities)
        self.assertIn(live_timer_entity_id, self.registry.entities)

        self.runtime._schedules = []
        self.runtime._timers = []
        fire_switch_signal()
        self.assertNotIn(live_schedule_entity_id, self.registry.entities)
        self.assertNotIn(live_timer_entity_id, self.registry.entities)

    def test_number_re_adds_slider_when_its_registry_entry_disappears(self) -> None:
        player = "media_player.x"
        self.runtime._volume_rules = [{"player": player, "max_volume": 40, "enabled": True}]

        number_added, fire_number_signal = self._setup(self.number)
        slider = next(e for e in number_added if getattr(e, "_player", None) == player)
        # No registry entry was ever created for the slider (or it went missing, e.g. by
        # the pre-fix cross-platform deletion bug). It must not be stuck forever.
        number_added.clear()
        fire_number_signal()

        recreated = [e for e in number_added if getattr(e, "_player", None) == player]
        self.assertEqual(len(recreated), 1)
        self.assertEqual(recreated[0]._attr_unique_id, slider._attr_unique_id)


if __name__ == "__main__":
    main()
