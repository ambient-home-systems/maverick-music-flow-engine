"""Schedules on daylight-saving change days run once (B-11).

Home Assistant's test time zone is US/Pacific. In 2026 its clocks jump from 02:00 to
03:00 on 8 March and go back from 02:00 to 01:00 on 1 November. A schedule time the
clocks skip runs at the first valid minute after the gap; a time that happens twice runs
at its first occurrence only. Due times are compared as elapsed time, so the Engine's
schedulers (the 30-second and minute ticks, the schedule's exact timer and its switch's
timer) agree on a single run.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import KITCHEN, engine_runtime
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.maverick_music_flow.const import DOMAIN
from custom_components.maverick_music_flow.runtime import (
    _due_schedule_datetime,
    _next_schedule_datetime,
)


def local(*args: int) -> datetime:
    """Return a datetime in Home Assistant's time zone (US/Pacific in tests)."""
    return datetime(*args, tzinfo=dt_util.get_default_time_zone())


async def _start_at(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    when: datetime,
) -> list[str]:
    """Move the clock to ``when``; return the list that schedule runs are recorded in.

    Each run records the local time it executed instead of playing media.
    """
    runtime = engine_runtime(hass)
    # Nothing here needs Music Assistant, and the clock jumps would trip its heartbeat.
    await runtime._music_assistant_client.async_stop()
    # Restart the Engine's ticks after the jump: timers set before a jump back never fire.
    runtime.async_stop_orchestration()
    freezer.move_to(when)
    runtime.async_start_orchestration()
    await hass.async_block_till_done()
    runs: list[str] = []

    async def execute(schedule: dict[str, Any]) -> dict[str, Any]:
        runs.append(dt_util.now().isoformat())
        return {"ok": True, "executed_at": dt_util.utcnow().isoformat()}

    monkeypatch.setattr(runtime, "async_execute_schedule", execute)
    return runs


async def _set_schedule(hass: HomeAssistant, time: str) -> None:
    await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {"id": "wake", "player": KITCHEN, "media_id": "library://playlist/1", "time": time},
        blocking=True,
    )
    await hass.async_block_till_done()


async def _walk_to(hass: HomeAssistant, freezer: FrozenDateTimeFactory, end: datetime) -> None:
    """Move the clock a minute at a time until ``end``, firing every timer on the way."""
    while dt_util.utcnow() < end:
        freezer.tick(timedelta(minutes=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


def _next_run(hass: HomeAssistant) -> str:
    return engine_runtime(hass).schedule_summaries("default")[0]["next_run"]


async def test_time_skipped_by_spring_forward_runs_once_after_the_gap(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """02:30 does not exist on 8 March: the schedule runs once, at 03:00 PDT."""
    runs = await _start_at(hass, freezer, monkeypatch, local(2026, 3, 8, 1, 30))
    await _set_schedule(hass, "02:30")
    assert _next_run(hass) == "2026-03-08T03:00:00-07:00"

    await _walk_to(hass, freezer, local(2026, 3, 8, 4, 0))

    assert runs == ["2026-03-08T03:00:00-07:00"]
    assert _next_run(hass) == "2026-03-09T02:30:00-07:00"


async def test_time_repeated_by_fall_back_runs_once(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """01:30 happens twice on 1 November: the schedule runs once, at 01:30 PDT."""
    runs = await _start_at(hass, freezer, monkeypatch, local(2026, 11, 1, 1, 0))
    await _set_schedule(hass, "01:30")
    assert _next_run(hass) == "2026-11-01T01:30:00-07:00"

    # 01:10 PST, in the repeated hour. Forget the earlier run, as a restart does, and
    # save the schedule again so every scheduler plans its next run from here.
    await _walk_to(hass, freezer, datetime(2026, 11, 1, 9, 10, tzinfo=UTC))
    engine_runtime(hass)._last_schedule_runs.clear()
    await _set_schedule(hass, "01:30")
    assert _next_run(hass) == "2026-11-02T01:30:00-08:00"

    await _walk_to(hass, freezer, local(2026, 11, 1, 2, 10))

    assert runs == ["2026-11-01T01:30:00-07:00"]


@pytest.mark.parametrize(
    ("zone", "day", "time", "expected"),
    [
        ("America/Los_Angeles", date(2026, 3, 8), "02:00", "2026-03-08T03:00:00-07:00"),
        ("America/Los_Angeles", date(2026, 3, 8), "02:59", "2026-03-08T03:00:00-07:00"),
        ("America/Los_Angeles", date(2026, 3, 8), "03:00", "2026-03-08T03:00:00-07:00"),
        ("Europe/Berlin", date(2026, 3, 29), "02:30", "2026-03-29T03:00:00+02:00"),
        # Lord Howe Island moves its clocks by 30 minutes, from 02:00 to 02:30.
        ("Australia/Lord_Howe", date(2026, 10, 4), "02:10", "2026-10-04T02:30:00+11:00"),
        ("America/Los_Angeles", date(2026, 11, 1), "01:30", "2026-11-01T01:30:00-07:00"),
        ("Europe/Berlin", date(2026, 10, 25), "02:30", "2026-10-25T02:30:00+02:00"),
    ],
)
def test_next_run_on_daylight_saving_days(zone: str, day: date, time: str, expected: str) -> None:
    """Skipped times move to the end of the gap; repeated times use the first occurrence."""
    midnight = datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(zone))
    next_run = _next_schedule_datetime({"time": time}, midnight)
    assert next_run is not None
    assert next_run.isoformat() == expected


def test_repeated_time_is_due_only_the_first_time() -> None:
    """At 01:30 PST, an hour after 01:30 PDT, the 01:30 run is no longer due."""
    zone = ZoneInfo("America/Los_Angeles")
    schedule = {"time": "01:30"}
    first = datetime(2026, 11, 1, 1, 30, 30, tzinfo=zone)
    second = datetime(2026, 11, 1, 1, 30, 30, fold=1, tzinfo=zone)

    due = _due_schedule_datetime(schedule, first)
    assert due is not None
    assert due.isoformat() == "2026-11-01T01:30:00-07:00"
    assert _due_schedule_datetime(schedule, second) is None


def test_due_window_is_two_minutes_of_elapsed_time() -> None:
    """A 01:59 run stays due until 02:01 PDT, which is 01:01 PST."""
    zone = ZoneInfo("America/Los_Angeles")
    schedule = {"time": "01:59"}
    assert _due_schedule_datetime(schedule, datetime(2026, 11, 1, 1, 0, 59, fold=1, tzinfo=zone))
    assert (
        _due_schedule_datetime(schedule, datetime(2026, 11, 1, 1, 1, fold=1, tzinfo=zone)) is None
    )
