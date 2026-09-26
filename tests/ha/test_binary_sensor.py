"""Binary sensor platform: connection health, playback and volume policy."""

from __future__ import annotations

from conftest import BEDROOM, KITCHEN, async_press_button, engine_entity_id
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maverick_music_flow.binary_sensor import BINARY_SENSORS
from custom_components.maverick_music_flow.const import DOMAIN


def _state(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str:
    return hass.states.get(engine_entity_id(hass, entry, "binary_sensor", key)).state


async def test_binary_sensors_created_with_initial_state(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Every description becomes an entity; health is on for a working MA server."""
    states = {
        description.key: _state(hass, loaded_entry, description.key)
        for description in BINARY_SENSORS
    }
    assert states == {
        "required_connections_ok": STATE_ON,
        "music_assistant_connected": STATE_ON,
        "queue_provider_connected": STATE_ON,
        "library_provider_connected": STATE_ON,
        "search_provider_connected": STATE_ON,
        "any_player_playing": STATE_ON,
        "group_active": STATE_OFF,
        "volume_policy_active": STATE_OFF,
    }


async def test_volume_policy_follows_volume_rules(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """An enabled all-day rule turns the volume policy on; deleting it turns it off."""
    await hass.services.async_call(
        DOMAIN, "set_volume_rule", {"player": BEDROOM, "max_volume": 90}, blocking=True
    )
    await hass.async_block_till_done()
    assert _state(hass, loaded_entry, "volume_policy_active") == STATE_ON

    await hass.services.async_call(DOMAIN, "delete_volume_rule", {"player": BEDROOM}, blocking=True)
    await hass.async_block_till_done()
    assert _state(hass, loaded_entry, "volume_policy_active") == STATE_OFF


async def test_playback_sensor_follows_player_state(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Pausing the only playing player turns "any player playing" off."""
    hass.states.async_set(KITCHEN, "paused", hass.states.get(KITCHEN).attributes)
    await async_press_button(hass, loaded_entry, "refresh_state")
    assert _state(hass, loaded_entry, "any_player_playing") == STATE_OFF
