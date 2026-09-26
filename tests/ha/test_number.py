"""Number platform: screensaver timeout and volume-rule maximum sliders."""

from __future__ import annotations

import pytest
from conftest import KITCHEN, MusicAssistantStub, engine_entity_id, engine_runtime
from homeassistant.components.number import ATTR_VALUE, SERVICE_SET_VALUE
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maverick_music_flow.const import DOMAIN


async def _set_value(hass: HomeAssistant, entity_id: str, value: float) -> None:
    await hass.services.async_call(
        "number",
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value},
        blocking=True,
    )
    await hass.async_block_till_done()


async def test_screensaver_timeout_number(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """The timeout starts at 90 seconds, can be changed and stays within its range."""
    entity_id = engine_entity_id(hass, loaded_entry, "number", "system_screensaver_timeout")
    assert hass.states.get(entity_id).state == "90.0"

    await _set_value(hass, entity_id, 300)
    assert hass.states.get(entity_id).state == "300.0"
    assert engine_runtime(hass).screensaver_config("default")["timeout_seconds"] == 300

    with pytest.raises(ServiceValidationError):
        await _set_value(hass, entity_id, 5)
    assert hass.states.get(entity_id).state == "300.0"


async def test_volume_rule_number_created_updated_removed(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    mock_music_assistant: MusicAssistantStub,
    entity_registry: er.EntityRegistry,
) -> None:
    """The Max volume slider edits its rule, which is enforced on the player."""
    await hass.services.async_call(
        DOMAIN, "set_volume_rule", {"player": KITCHEN, "max_volume": 50}, blocking=True
    )
    await hass.async_block_till_done()
    entity_id = engine_entity_id(hass, loaded_entry, "number", f"volume_rule_max_{KITCHEN}")
    state = hass.states.get(entity_id)
    assert state.state == "50.0"
    assert state.name == "HOMEii Flow Engine Max volume: Kitchen"
    volume_calls = mock_music_assistant.service_calls["media_player.volume_set"]
    assert volume_calls == []  # The kitchen is at 30%, below the limit.

    await _set_value(hass, entity_id, 20)
    assert hass.states.get(entity_id).state == "20.0"
    assert engine_runtime(hass).volume_rules("default")[0]["max_volume"] == 20
    assert [call.data for call in volume_calls] == [{"entity_id": KITCHEN, "volume_level": 0.2}]

    await hass.services.async_call(DOMAIN, "clear_volume_rules", {}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id) is None
    assert entity_registry.async_get(entity_id) is None
