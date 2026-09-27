"""Schedule execution: a command Music Assistant accepted is never sent again (B-3).

Only a command MA did not accept is retried. Once MA accepts it, re-sending would play or
queue the media again, so the schedule keeps checking the queue instead.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from conftest import KITCHEN, KITCHEN_ID, FakeCommandError, FakeMusicAssistant, engine_runtime
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maverick_music_flow import runtime as runtime_module
from custom_components.maverick_music_flow.const import DOMAIN


@pytest.fixture(autouse=True)
def short_play_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make play_media's own confirmation window two instant polls."""
    monkeypatch.setattr(runtime_module, "_PLAY_VERIFY_POLLS", 2)
    monkeypatch.setattr(runtime_module, "_PLAY_VERIFY_INTERVAL", 0)


async def _run_schedule(hass: HomeAssistant, **schedule: Any) -> dict[str, Any]:
    """Store a schedule, run it now and return the recorded result."""
    await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {
            "id": "wake",
            "name": "Wake up",
            "player": KITCHEN,
            "media_id": "library://playlist/1",
            "media_type": "playlist",
            "time": "07:00",
            "retry_delay": 1,
            **schedule,
        },
        blocking=True,
    )
    await hass.services.async_call(DOMAIN, "run_schedule", {"id": "wake"}, blocking=True)
    return engine_runtime(hass)._last_schedule_action


@pytest.mark.parametrize("enqueue", ["add", "next"])
async def test_enqueue_runs_once_and_is_ok(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, fake_ma: FakeMusicAssistant, enqueue: str
) -> None:
    """add/next is sent once and verified by the queue growing."""
    result = await _run_schedule(hass, enqueue=enqueue)

    assert fake_ma.commands_named("player_queues/play_media") == [
        {
            "queue_id": KITCHEN_ID,
            "media": "library://playlist/1",
            "option": enqueue,
            "radio_mode": False,
        }
    ]
    assert result["ok"] is True
    assert result["verified"] is True
    assert result["verification"] == "verified"
    assert result["error"] == ""
    assert len(result["schedule_attempts"]) == 1
    assert engine_runtime(hass).last_activity("default")["kind"] == "schedule_executed"


async def test_enqueue_without_queue_count_is_skipped_and_ok(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, fake_ma: FakeMusicAssistant
) -> None:
    """When MA reports no queue count there is nothing to check: one send, recorded ok."""
    fake_ma.responses["player_queues/get_active_queue"] = {"queue_id": KITCHEN_ID}

    result = await _run_schedule(hass, enqueue="add", retry_attempts=4)

    assert len(fake_ma.commands_named("player_queues/play_media")) == 1
    assert fake_ma.commands_named("player_queues/get") == []
    assert result["ok"] is True
    assert result["verified"] is False
    assert result["verification"] == "skipped"
    assert engine_runtime(hass).last_activity("default")["kind"] == "schedule_executed"


async def test_failed_command_is_retried(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, fake_ma: FakeMusicAssistant
) -> None:
    """A play command MA rejected was not accepted, so it is sent again."""
    default_result = fake_ma.default_result
    rejected: list[dict[str, Any]] = []

    def play_media(args: dict[str, Any]) -> Any:
        if not rejected:
            rejected.append(args)
            raise FakeCommandError("Player is busy")
        return default_result("player_queues/play_media", args)

    fake_ma.responses["player_queues/play_media"] = play_media

    result = await _run_schedule(hass, enqueue="play", retry_attempts=3)

    assert len(fake_ma.commands_named("player_queues/play_media")) == 2
    assert result["ok"] is True
    assert result["verification"] == "verified"
    assert [attempt["ok"] for attempt in result["schedule_attempts"]] == [False, True]
    assert "Player is busy" in result["schedule_attempts"][0]["error"]


async def test_slow_play_verification_does_not_replay(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, fake_ma: FakeMusicAssistant
) -> None:
    """MA accepted the play but confirms it late: keep checking, never replay."""
    old_queue = copy.deepcopy(fake_ma.queues[KITCHEN_ID])
    default_result = fake_ma.default_result

    def get_queue(args: dict[str, Any]) -> Any:
        # play_media's own two polls and the schedule's first delayed check see the old
        # queue; the next check sees the new item.
        if len(fake_ma.commands_named("player_queues/get")) <= 3:
            return old_queue
        return default_result("player_queues/get", args)

    fake_ma.responses["player_queues/get"] = get_queue

    result = await _run_schedule(hass, enqueue="play", retry_attempts=4)

    assert len(fake_ma.commands_named("player_queues/play_media")) == 1
    assert len(fake_ma.commands_named("player_queues/get")) == 4
    assert result["ok"] is True
    assert result["verification"] == "verified"
    assert len(result["schedule_attempts"]) == 1
    assert result["schedule_attempts"][0]["delayed_verified"] is True
    assert engine_runtime(hass).last_activity("default")["kind"] == "schedule_executed"


async def test_unconfirmed_play_fails_without_replaying(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, fake_ma: FakeMusicAssistant
) -> None:
    """MA accepted the play but never confirms it: recorded as failed, sent only once."""
    old_queue = copy.deepcopy(fake_ma.queues[KITCHEN_ID])
    fake_ma.responses["player_queues/get"] = old_queue

    result = await _run_schedule(hass, enqueue="play", retry_attempts=2)

    assert len(fake_ma.commands_named("player_queues/play_media")) == 1
    # Two polls inside play_media, then one delayed check for the second attempt.
    assert len(fake_ma.commands_named("player_queues/get")) == 3
    assert result["ok"] is False
    assert result["verification"] == "unconfirmed"
    assert "not re-sent" in result["error"]
    assert engine_runtime(hass).last_activity("default")["kind"] == "schedule_failed"
