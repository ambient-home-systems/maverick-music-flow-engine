"""Switch platform: system screensaver, schedules, timers and volume rules."""

from __future__ import annotations

from conftest import (
    BEDROOM,
    KITCHEN,
    async_engine_updated,
    engine_entity_id,
    engine_runtime,
)
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maverick_music_flow.const import DOMAIN


async def _switch(hass: HomeAssistant, entity_id: str, service: str) -> None:
    await hass.services.async_call(
        SWITCH_DOMAIN, service, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    await hass.async_block_till_done()


async def test_system_screensaver_switch(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """The screensaver switch is created off and turns the screensaver on and off."""
    entity_id = engine_entity_id(hass, loaded_entry, "switch", "system_screensaver")
    assert entity_id == "switch.homeii_flow_engine_system_screensaver"
    assert hass.states.get(entity_id).state == STATE_OFF

    await _switch(hass, entity_id, SERVICE_TURN_ON)
    assert hass.states.get(entity_id).state == STATE_ON
    assert engine_runtime(hass).screensaver_config("default")["enabled"] is True

    await _switch(hass, entity_id, SERVICE_TURN_OFF)
    assert hass.states.get(entity_id).state == STATE_OFF
    assert engine_runtime(hass).screensaver_config("default")["enabled"] is False


async def test_schedule_switch_created_updated_removed(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    """A schedule gets a switch, which disables it, and loses it when deleted."""
    await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {
            "id": "wake",
            "name": "Wake up",
            "player": KITCHEN,
            "media_id": "library://playlist/1",
            "time": "07:00",
        },
        blocking=True,
    )
    await hass.async_block_till_done()
    entity_id = engine_entity_id(hass, loaded_entry, "switch", "schedule_wake")
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.name == "HOMEii Flow Engine Wake up"
    assert state.attributes["time"] == "07:00"
    assert state.attributes["player"] == KITCHEN

    await _switch(hass, entity_id, SERVICE_TURN_OFF)
    assert hass.states.get(entity_id).state == STATE_OFF
    assert engine_runtime(hass).schedules("default")[0]["enabled"] is False

    await hass.services.async_call(DOMAIN, "delete_schedule", {"id": "wake"}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id) is None
    assert entity_registry.async_get(entity_id) is None


async def test_timer_switch_created_updated_removed(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    """A timer gets a switch, which disables it, and loses it when deleted."""
    await hass.services.async_call(
        DOMAIN,
        "set_timer",
        {"id": "nap", "player": KITCHEN, "minutes": 30, "action": "pause"},
        blocking=True,
    )
    await hass.async_block_till_done()
    entity_id = engine_entity_id(hass, loaded_entry, "switch", "timer_nap")
    assert entity_id is not None
    assert hass.states.get(entity_id).state == STATE_ON

    await _switch(hass, entity_id, SERVICE_TURN_OFF)
    assert hass.states.get(entity_id).state == STATE_OFF
    assert engine_runtime(hass).timers("default")[0]["enabled"] is False

    await hass.services.async_call(DOMAIN, "delete_timer", {"id": "nap"}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id) is None
    assert entity_registry.async_get(entity_id) is None


async def test_volume_rule_switch_and_slider_survive_engine_updates(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    """Regression test for B-1: switch cleanup must not delete the Max volume slider."""
    await hass.services.async_call(
        DOMAIN, "set_volume_rule", {"player": BEDROOM, "max_volume": 90}, blocking=True
    )
    await hass.async_block_till_done()
    switch_id = engine_entity_id(hass, loaded_entry, "switch", f"volume_rule_{BEDROOM}")
    number_id = engine_entity_id(hass, loaded_entry, "number", f"volume_rule_max_{BEDROOM}")
    assert switch_id is not None
    assert number_id is not None

    for _ in range(3):
        await async_engine_updated(hass)
    assert entity_registry.async_get(switch_id) is not None
    assert entity_registry.async_get(number_id) is not None
    assert hass.states.get(number_id).state == "90.0"

    # A reload runs every platform's cleanup again.
    assert await hass.config_entries.async_reload(loaded_entry.entry_id)
    await hass.async_block_till_done()
    assert entity_registry.async_get(number_id) is not None
    assert hass.states.get(number_id).state == "90.0"

    await _switch(hass, switch_id, SERVICE_TURN_OFF)
    assert hass.states.get(switch_id).state == STATE_OFF
    assert engine_runtime(hass).volume_rules("default")[0]["enabled"] is False

    await hass.services.async_call(DOMAIN, "delete_volume_rule", {"player": BEDROOM}, blocking=True)
    await async_engine_updated(hass)
    assert entity_registry.async_get(switch_id) is None
    assert entity_registry.async_get(number_id) is None
