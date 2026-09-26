"""Sensor platform: connection health, player statistics and stored items."""

from __future__ import annotations

from datetime import datetime

from conftest import BEDROOM, KITCHEN, MA_TOKEN, async_press_button, engine_entity_id
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maverick_music_flow.const import DOMAIN
from custom_components.maverick_music_flow.sensor import SENSORS


def _sensor_states(hass: HomeAssistant, entry: MockConfigEntry, *keys: str) -> dict[str, str]:
    return {
        key: hass.states.get(engine_entity_id(hass, entry, "sensor", key)).state for key in keys
    }


async def test_every_sensor_is_created(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> None:
    """Each sensor description becomes one entity with a state."""
    for description in SENSORS:
        entity_id = engine_entity_id(hass, loaded_entry, "sensor", description.key)
        assert entity_id is not None, description.key
        assert hass.states.get(entity_id) is not None, entity_id


async def test_sensors_report_players_and_connections(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Initial values come from the MA players in Home Assistant and the MA probe."""
    assert _sensor_states(
        hass,
        loaded_entry,
        "status",
        "required_connections",
        "required_connection_music_assistant",
        "required_connection_queue",
        "required_connection_library",
        "required_connection_search",
        "players_total",
        "players_playing",
        "active_player",
        "players_grouped",
        "music_assistant_players",
        "schedules",
        "next_schedule",
        "timers",
        "next_timer",
        "volume_rules",
    ) == {
        "status": "connected",
        "required_connections": "healthy",
        "required_connection_music_assistant": "connected",
        "required_connection_queue": "connected",
        "required_connection_library": "connected",
        "required_connection_search": "connected",
        "players_total": "2",
        "players_playing": "1",
        "active_player": KITCHEN,
        "players_grouped": "0",
        "music_assistant_players": "2",
        "schedules": "0",
        "next_schedule": "none",
        "timers": "0",
        "next_timer": "none",
        "volume_rules": "0",
    }
    playing = hass.states.get(engine_entity_id(hass, loaded_entry, "sensor", "players_playing"))
    assert playing.attributes["entities"] == [KITCHEN]
    # The status sensor's attributes are the card context; they never carry the token.
    status = hass.states.get(engine_entity_id(hass, loaded_entry, "sensor", "status"))
    assert status.attributes["engine_version"]
    assert MA_TOKEN not in repr(status.attributes)


async def test_sensors_follow_player_state_changes(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Player statistics are recomputed from the Home Assistant player states."""
    kitchen = hass.states.get(KITCHEN)
    hass.states.async_set(KITCHEN, "paused", kitchen.attributes)
    bedroom = hass.states.get(BEDROOM)
    # Both players now share the kitchen queue, which the Engine counts as a group.
    hass.states.async_set(BEDROOM, "playing", {**bedroom.attributes, "active_queue": "ma_kitchen"})
    await async_press_button(hass, loaded_entry, "refresh_state")

    assert _sensor_states(
        hass, loaded_entry, "players_playing", "active_player", "players_grouped"
    ) == {
        "players_playing": "1",
        "active_player": BEDROOM,
        "players_grouped": "2",
    }


async def test_sensors_follow_stored_items(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Schedule, timer and volume-rule sensors update when items are stored."""
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
    await hass.services.async_call(
        DOMAIN, "set_timer", {"player": KITCHEN, "minutes": 45}, blocking=True
    )
    await hass.services.async_call(
        DOMAIN, "set_volume_rule", {"player": BEDROOM, "max_volume": 90}, blocking=True
    )
    await hass.async_block_till_done()

    states = _sensor_states(
        hass, loaded_entry, "schedules", "timers", "volume_rules", "next_schedule", "next_timer"
    )
    assert states["schedules"] == "1"
    assert states["timers"] == "1"
    assert states["volume_rules"] == "1"
    next_run = datetime.fromisoformat(states["next_schedule"])
    assert dt_util.as_local(next_run).strftime("%H:%M") == "07:00"
    ends_at = datetime.fromisoformat(states["next_timer"])
    assert 44 * 60 <= (ends_at - dt_util.utcnow()).total_seconds() <= 45 * 60
    next_schedule = hass.states.get(engine_entity_id(hass, loaded_entry, "sensor", "next_schedule"))
    assert next_schedule.attributes["next_schedule"]["name"] == "Wake up"

    last_activity = hass.states.get(engine_entity_id(hass, loaded_entry, "sensor", "last_activity"))
    assert last_activity.state == f"Volume rule saved for {BEDROOM}"
