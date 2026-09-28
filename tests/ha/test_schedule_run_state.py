"""Schedule run state survives restarts and edits made while a schedule runs (B-12, B-13).

- The last run of each schedule is saved, so a restart inside the two-minute due window
  does not play it again.
- after_run=disable only turns off the stored schedule. An edit saved while the run was
  waiting to retry is kept, and a schedule deleted during its run is not recreated. The
  schedule's switch runs through the same runner (H-5f).
"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from conftest import KITCHEN, engine_entity_id, engine_runtime
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.maverick_music_flow import runtime as runtime_module
from custom_components.maverick_music_flow.const import DOMAIN, STORAGE_KEY


def local(*args: int) -> datetime:
    """Return a datetime in Home Assistant's time zone (US/Pacific in tests)."""
    return datetime(*args, tzinfo=dt_util.get_default_time_zone())


def _record_runs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the local time of every schedule run instead of playing media.

    Patched on the class so an Engine runtime created after a restart records too.
    """
    runs: list[str] = []

    async def execute(self: Any, schedule: dict[str, Any]) -> dict[str, Any]:
        runs.append(dt_util.now().isoformat())
        return {"ok": True, "executed_at": dt_util.utcnow().isoformat()}

    monkeypatch.setattr(runtime_module.HomeiiFlowRuntime, "async_execute_schedule", execute)
    return runs


async def _set_schedule(hass: HomeAssistant, **schedule: Any) -> None:
    await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {
            "id": "wake",
            "name": "Wake up",
            "player": KITCHEN,
            "media_id": "library://playlist/1",
            "time": "07:00",
            **schedule,
        },
        blocking=True,
    )
    await hass.async_block_till_done()


async def _walk_to(hass: HomeAssistant, freezer: FrozenDateTimeFactory, end: datetime) -> None:
    """Move the clock 30 seconds at a time until ``end``, firing every timer on the way."""
    while dt_util.utcnow() < end:
        freezer.tick(timedelta(seconds=30))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def _start_engine_at(
    hass: HomeAssistant, entry: MockConfigEntry, freezer: FrozenDateTimeFactory, when: datetime
) -> None:
    """Load the entry if needed and restart the Engine's ticks at ``when``."""
    if entry.state is not ConfigEntryState.LOADED:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    runtime = engine_runtime(hass)
    # Nothing here needs Music Assistant, and the clock jumps would trip its heartbeat.
    await runtime._music_assistant_client.async_stop()
    runtime.async_stop_orchestration()
    freezer.move_to(when)
    runtime.async_start_orchestration()
    await hass.async_block_till_done()


async def test_restart_right_after_a_run_does_not_replay_it(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    hass_storage: dict[str, Any],
) -> None:
    """HA stops without a clean shutdown 30 seconds after a 07:00 run and starts again."""
    runs = _record_runs(monkeypatch)
    await _start_engine_at(hass, loaded_entry, freezer, local(2026, 9, 28, 6, 58))
    await _set_schedule(hass)

    await _walk_to(hass, freezer, local(2026, 9, 28, 7, 0, 30))
    assert runs == ["2026-09-28T07:00:00-07:00"]

    # What is on disk right now; the clean shutdown below would save everything again.
    on_disk = copy.deepcopy(hass_storage[STORAGE_KEY])
    assert on_disk["data"]["schedule_last_runs"] == {
        "default:wake": "default:wake:2026-09-28:07:00"
    }
    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()
    hass_storage[STORAGE_KEY] = on_disk
    hass.data[DOMAIN].pop("runtime")

    await _start_engine_at(hass, loaded_entry, freezer, local(2026, 9, 28, 7, 1))
    # Past the scheduler's ready delay and the end of the due window.
    await _walk_to(hass, freezer, local(2026, 9, 28, 7, 3))

    assert runs == ["2026-09-28T07:00:00-07:00"]
    assert engine_runtime(hass)._last_schedule_check["trigger"] != "catchup"


async def test_restart_keeps_only_current_schedules_last_runs(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    hass_storage: dict[str, Any],
) -> None:
    """Stored last runs are one per schedule; deleted schedules' runs are dropped."""
    _record_runs(monkeypatch)
    runtime = engine_runtime(hass)
    for schedule_id in ("wake", "nap"):
        await _set_schedule(hass, id=schedule_id)
        await runtime.async_run_scheduled_schedule("default", schedule_id, local(2026, 9, 28, 7))
    await runtime.async_run_scheduled_schedule("default", "wake", local(2026, 9, 29, 7))
    await hass.services.async_call(DOMAIN, "delete_schedule", {"id": "nap"}, blocking=True)
    await _set_schedule(hass, id="late")
    await runtime.async_run_scheduled_schedule("default", "late", local(2026, 9, 29, 7))

    assert hass_storage[STORAGE_KEY]["data"]["schedule_last_runs"] == {
        "default:wake": "default:wake:2026-09-29:07:00",
        "default:late": "default:late:2026-09-29:07:00",
    }


async def _fire_through_runner(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await engine_runtime(hass).async_run_scheduled_schedule("default", "wake", dt_util.now())


async def _fire_through_switch(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entity_id = engine_entity_id(hass, entry, SWITCH_DOMAIN, "schedule_wake")
    switch = hass.data[SWITCH_DOMAIN].get_entity(entity_id)
    await switch._async_fire(dt_util.now(), "switch_timer")
    await hass.async_block_till_done()


FIRE_PATHS = pytest.mark.parametrize(
    "fire", [_fire_through_runner, _fire_through_switch], ids=["runner", "switch"]
)


def _during_retry_wait(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    action: Callable[[], Awaitable[None]],
) -> None:
    """Make the player not ready on the first attempt and run ``action`` while waiting."""
    runtime = engine_runtime(hass)
    readiness = runtime._player_readiness
    calls: list[str] = []

    def player_readiness(player: str) -> dict[str, Any]:
        calls.append(player)
        if len(calls) == 1:
            return {"ready": False, "reason": "player is starting"}
        return readiness(player)

    async def wait(self: Any, player: str, seconds: int) -> None:
        await action()

    monkeypatch.setattr(runtime, "_player_readiness", player_readiness)
    monkeypatch.setattr(runtime_module.HomeiiScheduleActionQueue, "_wait_for_player_or_delay", wait)


@FIRE_PATHS
async def test_run_once_schedule_is_disabled_after_it_runs(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    fire: Callable[[HomeAssistant, MockConfigEntry], Awaitable[None]],
) -> None:
    """after_run=disable turns the schedule off and changes nothing else."""
    runs = _record_runs(monkeypatch)
    await _set_schedule(hass, after_run="disable")
    before = copy.deepcopy(engine_runtime(hass).schedules("default")[0])

    await fire(hass, loaded_entry)

    assert len(runs) == 1
    after = engine_runtime(hass).schedules("default")
    assert len(after) == 1
    assert after[0]["enabled"] is False
    assert after[0]["updated_at"] != before["updated_at"]
    changed = {"enabled", "updated_at"}
    assert {key: value for key, value in after[0].items() if key not in changed} == {
        key: value for key, value in before.items() if key not in changed
    }


@FIRE_PATHS
async def test_edit_made_during_retries_survives(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    hass_storage: dict[str, Any],
    fire: Callable[[HomeAssistant, MockConfigEntry], Awaitable[None]],
) -> None:
    """The schedule is edited while its run waits to retry: the edit is kept as saved."""
    runs = _record_runs(monkeypatch)
    await _set_schedule(hass, after_run="disable")

    async def edit() -> None:
        await _set_schedule(hass, name="Wake up later", time="08:15", after_run="disable")

    _during_retry_wait(hass, monkeypatch, edit)

    await fire(hass, loaded_entry)

    assert len(runs) == 1
    stored = hass_storage[STORAGE_KEY]["data"]["schedules"]
    assert len(stored) == 1
    assert stored[0]["name"] == "Wake up later"
    assert stored[0]["time"] == "08:15"
    assert stored[0]["enabled"] is True
    assert engine_runtime(hass).schedules("default") == stored


@FIRE_PATHS
async def test_schedule_deleted_during_its_run_is_not_recreated(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    hass_storage: dict[str, Any],
    fire: Callable[[HomeAssistant, MockConfigEntry], Awaitable[None]],
) -> None:
    """The schedule is deleted while its run waits to retry: it stays deleted."""
    runs = _record_runs(monkeypatch)
    await _set_schedule(hass, after_run="disable")

    async def delete() -> None:
        await hass.services.async_call(DOMAIN, "delete_schedule", {"id": "wake"}, blocking=True)

    _during_retry_wait(hass, monkeypatch, delete)

    await fire(hass, loaded_entry)
    await hass.async_block_till_done()

    assert len(runs) == 1
    assert engine_runtime(hass).schedules() == []
    assert hass_storage[STORAGE_KEY]["data"]["schedules"] == []
    assert engine_entity_id(hass, loaded_entry, SWITCH_DOMAIN, "schedule_wake") is None
