"""Button platform: Engine actions and per-schedule run buttons."""

from __future__ import annotations

from conftest import (
    KITCHEN,
    KITCHEN_ID,
    FakeMusicAssistant,
    MusicAssistantStub,
    async_press_button,
    engine_entity_id,
    engine_runtime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maverick_music_flow.button import BUTTONS
from custom_components.maverick_music_flow.const import DOMAIN

WAKE_SCHEDULE = {
    "id": "wake",
    "name": "Wake up",
    "player": KITCHEN,
    "media_id": "library://playlist/1",
    "media_type": "playlist",
    "time": "07:00",
}


async def test_engine_buttons_are_created(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Each fixed Engine action has a button."""
    for description in BUTTONS:
        assert engine_entity_id(hass, loaded_entry, "button", description.key), description.key


async def test_run_orchestration_button(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> None:
    """The button runs one orchestration pass and records the press."""
    await async_press_button(hass, loaded_entry, "run_orchestration")
    runtime = engine_runtime(hass)
    assert runtime.orchestration_status()["last_tick_trigger"] == "button"
    assert runtime.orchestration_status()["last_button_action"]["action"] == "run_orchestration"
    assert runtime.last_activity("default")["message"] == "Button pressed: run_orchestration"


async def test_apply_volume_rules_button(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_music_assistant: MusicAssistantStub
) -> None:
    """The button lowers a player that is louder than its rule allows."""
    await hass.services.async_call(
        DOMAIN, "set_volume_rule", {"player": KITCHEN, "max_volume": 50}, blocking=True
    )
    volume_calls = mock_music_assistant.service_calls["media_player.volume_set"]
    assert volume_calls == []
    # Someone turns the kitchen up past the limit.
    hass.states.async_set(
        KITCHEN, "playing", {**hass.states.get(KITCHEN).attributes, "volume_level": 0.9}
    )

    await async_press_button(hass, loaded_entry, "apply_volume_rules")
    assert [call.data for call in volume_calls] == [{"entity_id": KITCHEN, "volume_level": 0.5}]


async def test_show_screensaver_button(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> None:
    """The button records a one-off request for dashboards to show the screensaver."""
    await async_press_button(hass, loaded_entry, "show_system_screensaver_now")
    config = engine_runtime(hass).screensaver_config("default")
    assert config["show_request_id"]
    assert config["show_source"] == "button"


async def test_run_next_schedule_button_without_schedules(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, fake_ma: FakeMusicAssistant
) -> None:
    """With nothing scheduled the button reports that and plays nothing."""
    await async_press_button(hass, loaded_entry, "run_next_schedule")
    action = engine_runtime(hass).orchestration_status()["last_button_action"]
    assert action["ok"] is False
    assert fake_ma.commands_named("player_queues/play_media") == []


async def test_schedule_run_buttons_created_pressed_removed(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    fake_ma: FakeMusicAssistant,
    entity_registry: er.EntityRegistry,
) -> None:
    """Each schedule gets a Run button that plays its media through Music Assistant."""
    await hass.services.async_call(DOMAIN, "set_schedule", WAKE_SCHEDULE, blocking=True)
    await hass.async_block_till_done()
    entity_id = engine_entity_id(hass, loaded_entry, "button", "run_schedule_wake")
    assert entity_id is not None

    await async_press_button(hass, loaded_entry, "run_schedule_wake")
    plays = fake_ma.commands_named("player_queues/play_media")
    assert plays == [
        {
            "queue_id": KITCHEN_ID,
            "media": "library://playlist/1",
            "option": "replace",
            "radio_mode": False,
        }
    ]
    assert engine_runtime(hass).orchestration_status()["last_button_action"]["ok"] is True

    # "Run next schedule now" picks the same schedule.
    await async_press_button(hass, loaded_entry, "run_next_schedule")
    assert len(fake_ma.commands_named("player_queues/play_media")) == 2

    await hass.services.async_call(DOMAIN, "delete_schedule", {"id": "wake"}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id) is None
    assert entity_registry.async_get(entity_id) is None
