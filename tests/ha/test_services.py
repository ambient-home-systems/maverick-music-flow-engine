"""Engine actions (services), including who may call them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import (
    BEDROOM,
    BEDROOM_ID,
    KITCHEN,
    KITCHEN_ID,
    FakeMusicAssistant,
    MusicAssistantStub,
    engine_runtime,
)
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockUser,
    async_mock_service,
)

from custom_components.maverick_music_flow.const import (
    CONF_ALLOW_NON_ADMIN_MANAGEMENT,
    CONF_ENABLE_EXPERIMENTAL,
    DOMAIN,
)

SERVICES_YAML = Path(__file__).parents[2] / "custom_components" / DOMAIN / "services.yaml"
WAKE_SCHEDULE = {
    "id": "wake",
    "name": "Wake up",
    "player": KITCHEN,
    "media_id": "library://playlist/1",
    "media_type": "playlist",
    "time": "07:00",
}


async def _call(
    hass: HomeAssistant, service: str, data: dict[str, Any], user: MockUser | None = None
) -> None:
    await hass.services.async_call(
        DOMAIN, service, data, blocking=True, context=Context(user_id=user.id) if user else None
    )
    await hass.async_block_till_done()


async def test_every_documented_service_is_registered(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """services.yaml and the registered actions match."""
    documented = set(yaml.safe_load(SERVICES_YAML.read_text(encoding="utf-8")))
    assert set(hass.services.async_services()[DOMAIN]) == documented


async def test_play_media_service(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, fake_ma: FakeMusicAssistant
) -> None:
    """play_media resolves the player's queue and plays through Music Assistant."""
    await _call(
        hass,
        "play_media",
        {"player": KITCHEN, "media_id": "library://playlist/1", "enqueue": "next"},
    )
    assert fake_ma.commands_named("player_queues/get_active_queue")[-1] == {"player_id": KITCHEN_ID}
    assert fake_ma.commands_named("player_queues/play_media") == [
        {
            "queue_id": KITCHEN_ID,
            "media": "library://playlist/1",
            "option": "next",
            "radio_mode": False,
        }
    ]


async def test_play_media_refuses_local_network_urls(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, fake_ma: FakeMusicAssistant
) -> None:
    """URL media on the local network is refused; the configured MA server is trusted (S-5)."""
    with pytest.raises(ServiceValidationError):
        await _call(
            hass, "play_media", {"player": KITCHEN, "media_id": "http://192.168.1.50/song.mp3"}
        )
    assert fake_ma.commands_named("player_queues/play_media") == []

    await _call(hass, "play_media", {"player": KITCHEN, "media_id": f"{fake_ma.url}/song.mp3"})
    assert (
        fake_ma.commands_named("player_queues/play_media")[0]["media"] == f"{fake_ma.url}/song.mp3"
    )


async def test_player_command_service(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, fake_ma: FakeMusicAssistant
) -> None:
    """Player commands map to the Music Assistant player API."""
    await _call(hass, "player_command", {"player": KITCHEN, "command": "pause"})
    await _call(hass, "player_command", {"player": BEDROOM, "command": "volume_set", "volume": 40})
    assert fake_ma.commands_named("players/cmd/pause") == [{"player_id": KITCHEN_ID}]
    assert fake_ma.commands_named("players/cmd/volume_set") == [
        {"player_id": BEDROOM_ID, "volume_level": 40}
    ]

    with pytest.raises(ServiceValidationError):
        await _call(hass, "player_command", {"player": KITCHEN, "command": "self_destruct"})


async def test_transfer_queue_service(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, fake_ma: FakeMusicAssistant
) -> None:
    """transfer_queue moves the kitchen queue to the bedroom."""
    await _call(hass, "transfer_queue", {"source_player": KITCHEN, "target_player": BEDROOM})
    assert fake_ma.commands_named("player_queues/transfer") == [
        {"source_queue_id": KITCHEN_ID, "target_queue_id": BEDROOM_ID, "auto_play": True}
    ]


async def test_schedule_services(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, fake_ma: FakeMusicAssistant
) -> None:
    """Schedules can be stored, run now and deleted."""
    await _call(hass, "set_schedule", WAKE_SCHEDULE)
    runtime = engine_runtime(hass)
    assert [schedule["id"] for schedule in runtime.schedules("default")] == ["wake"]

    await _call(hass, "run_schedule", {"id": "wake"})
    assert fake_ma.commands_named("player_queues/play_media")[0]["media"] == "library://playlist/1"
    assert runtime.orchestration_status()["last_schedule_action"]["ok"] is True

    await _call(hass, "delete_schedule", {"id": "wake"})
    assert runtime.schedules("default") == []


async def test_timer_and_volume_rule_services(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Timers and volume rules can be stored and deleted."""
    runtime = engine_runtime(hass)
    await _call(hass, "set_timer", {"player": KITCHEN, "minutes": 20, "action": "pause"})
    assert [(timer["player"], timer["action"]) for timer in runtime.timers("default")] == [
        (KITCHEN, "pause")
    ]
    await _call(hass, "delete_timer", {"player": KITCHEN})
    assert runtime.timers("default") == []

    await _call(
        hass,
        "set_volume_rule",
        {"player": BEDROOM, "max_volume": 90, "start_time": "22:00", "end_time": "06:00"},
    )
    assert runtime.volume_rules("default")[0]["start_time"] == "22:00"
    await _call(hass, "clear_volume_rules", {})
    assert runtime.volume_rules("default") == []


async def test_announce_url_uses_music_assistant(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    fake_ma: FakeMusicAssistant,
    mock_music_assistant: MusicAssistantStub,
) -> None:
    """An audio URL on the MA server is announced with music_assistant.play_announcement."""
    url = f"{fake_ma.url}/chime.mp3"
    await _call(hass, "announce", {"message": url, "player": KITCHEN, "volume": 35})
    calls = mock_music_assistant.service_calls["music_assistant.play_announcement"]
    assert len(calls) == 1
    assert calls[0].data["url"] == url
    assert calls[0].data["announce_volume"] == 35
    assert engine_runtime(hass).announcement_count("default") == 1


async def test_announce_text_uses_tts(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> None:
    """A text announcement uses the available TTS say service."""
    say_calls = async_mock_service(hass, "tts", "google_translate_say")
    await _call(
        hass,
        "announce",
        {"message": "Dinner is ready", "players": [KITCHEN, BEDROOM], "language": "en"},
    )
    assert [(call.data["entity_id"], call.data["message"]) for call in say_calls] == [
        (KITCHEN, "Dinner is ready"),
        (BEDROOM, "Dinner is ready"),
    ]


async def test_announce_refuses_local_network_urls(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_music_assistant: MusicAssistantStub
) -> None:
    """An announcement URL on the local network is refused (S-5)."""
    with pytest.raises(ServiceValidationError):
        await _call(hass, "announce", {"message": "http://10.0.0.5/alert.mp3", "player": KITCHEN})
    assert mock_music_assistant.service_calls["music_assistant.play_announcement"] == []


async def test_screensaver_and_preference_services(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Screensaver settings, show requests and interface preferences are stored."""
    runtime = engine_runtime(hass)
    await _call(hass, "set_screensaver", {"enabled": True, "timeout_seconds": 120, "mode": "clock"})
    config = runtime.screensaver_config("default")
    assert (config["enabled"], config["timeout_seconds"], config["mode"]) == (True, 120, "clock")

    await _call(hass, "show_screensaver", {})
    assert runtime.screensaver_config("default")["show_source"] == "service"

    await _call(hass, "set_interface_preferences", {"night_mode": "auto", "night_start": "22:30"})
    assert runtime.context()["interface_preferences"] == {
        "night_mode": "auto",
        "night_start": "22:30",
    }


async def test_set_queue_settings_service(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, fake_ma: FakeMusicAssistant
) -> None:
    """Queue settings are saved to Music Assistant and read back for confirmation."""
    await _call(hass, "set_queue_settings", {"crossfade_enabled": True, "crossfade_duration": 6})
    assert fake_ma.commands_named("config/core/save") == [
        {"domain": "player_queues", "values": {"crossfade_enabled": True, "crossfade_duration": 6}}
    ]
    assert fake_ma.queue_config["crossfade_duration"]["value"] == 6


async def test_run_orchestration_service(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """run_orchestration runs one pass."""
    await _call(hass, "run_orchestration", {})
    assert engine_runtime(hass).orchestration_status()["last_tick_trigger"] == "manual"


# --- authorization (S-3) -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("service", "data"),
    [
        ("set_schedule", WAKE_SCHEDULE),
        ("set_timer", {"player": KITCHEN, "minutes": 5}),
        ("set_volume_rule", {"player": KITCHEN, "max_volume": 10}),
        ("clear_volume_rules", {}),
        ("run_orchestration", {}),
        ("set_screensaver", {"enabled": True}),
        ("set_interface_preferences", {"night_mode": "on"}),
        ("set_queue_settings", {"crossfade_enabled": True}),
    ],
)
async def test_management_services_are_admin_only(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    kitchen_user: MockUser,
    hass_admin_user: MockUser,
    service: str,
    data: dict[str, Any],
) -> None:
    """A non-admin user is refused even for a player they control; an admin is not."""
    with pytest.raises(Unauthorized):
        await _call(hass, service, data, kitchen_user)
    await _call(hass, service, data, hass_admin_user)


async def test_playback_services_need_control_of_the_player(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    fake_ma: FakeMusicAssistant,
    kitchen_user: MockUser,
    hass_read_only_user: MockUser,
) -> None:
    """Playback actions follow Home Assistant's entity control permission."""
    await _call(hass, "player_command", {"player": KITCHEN, "command": "pause"}, kitchen_user)
    assert fake_ma.commands_named("players/cmd/pause") == [{"player_id": KITCHEN_ID}]

    with pytest.raises(Unauthorized):
        await _call(hass, "player_command", {"player": BEDROOM, "command": "pause"}, kitchen_user)
    with pytest.raises(Unauthorized):
        # The native MA player ID maps to the same entity permission.
        await _call(
            hass,
            "play_media",
            {"player": BEDROOM_ID, "media_id": "library://track/1"},
            kitchen_user,
        )
    with pytest.raises(Unauthorized):
        await _call(
            hass, "player_command", {"player": KITCHEN, "command": "pause"}, hass_read_only_user
        )
    assert len(fake_ma.commands_named("players/cmd/pause")) == 1
    assert fake_ma.commands_named("player_queues/play_media") == []


@pytest.mark.parametrize(
    "entry_options",
    [{CONF_ENABLE_EXPERIMENTAL: False, CONF_ALLOW_NON_ADMIN_MANAGEMENT: True}],
)
async def test_non_admin_management_option(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, kitchen_user: MockUser
) -> None:
    """With the option on, non-admins manage schedules for players they control."""
    await _call(hass, "set_schedule", WAKE_SCHEDULE, kitchen_user)
    assert [schedule["id"] for schedule in engine_runtime(hass).schedules("default")] == ["wake"]
    with pytest.raises(Unauthorized):
        await _call(
            hass, "set_schedule", {**WAKE_SCHEDULE, "id": "bed", "player": BEDROOM}, kitchen_user
        )
    with pytest.raises(Unauthorized):
        # Configuration stays admin-only.
        await _call(hass, "set_screensaver", {"enabled": True}, kitchen_user)
