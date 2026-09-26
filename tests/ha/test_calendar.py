"""Calendar platform: schedules as calendar events.

The tests marked ``xfail`` describe finding B-5 (calendar "next event" is the first
schedule in storage order, the calendar is never "on" during an event, and range
queries miss events that are in progress). Prompt 6.6 fixes it; the markers are strict,
so these tests fail once the fix lands until the markers are removed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from conftest import KITCHEN, async_engine_updated, engine_entity_id
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.calendar import DOMAIN as CALENDAR_DOMAIN
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maverick_music_flow.const import DOMAIN

B5 = pytest.mark.xfail(reason="B-5: fixed by Prompt 6.6", strict=True)


def local(*args: int) -> datetime:
    """Return a datetime in Home Assistant's time zone (US/Pacific in tests)."""
    return datetime(*args, tzinfo=dt_util.get_default_time_zone())


def _calendar_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    entity_id = engine_entity_id(hass, entry, "calendar", "schedules_calendar")
    assert entity_id is not None
    return entity_id


async def _set_schedule(
    hass: HomeAssistant, schedule_id: str, name: str, time: str, **extra: Any
) -> None:
    await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {
            "id": schedule_id,
            "name": name,
            "player": KITCHEN,
            "media_id": "library://playlist/1",
            "time": time,
            **extra,
        },
        blocking=True,
    )
    await hass.async_block_till_done()


async def _events(
    hass: HomeAssistant, entity_id: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    response = await hass.services.async_call(
        CALENDAR_DOMAIN,
        "get_events",
        {
            "entity_id": entity_id,
            "start_date_time": start.isoformat(),
            "end_date_time": end.isoformat(),
        },
        blocking=True,
        return_response=True,
    )
    return response[entity_id]["events"]


async def test_calendar_lists_schedule_occurrences(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """A weekday schedule appears once per matching day, and disabling removes it."""
    freezer.move_to(local(2026, 9, 28, 6, 0))  # A Monday.
    entity_id = _calendar_id(hass, loaded_entry)
    assert hass.states.get(entity_id).state == STATE_OFF

    # Days use Sunday = 0, so 1-5 is Monday to Friday.
    await _set_schedule(hass, "wake", "Wake up", "07:00", days=[1, 2, 3, 4, 5], volume=25)
    events = await _events(hass, entity_id, local(2026, 9, 27, 0, 0), local(2026, 10, 4, 0, 0))
    assert [event["start"] for event in events] == [
        local(2026, 9, day, 7, 0).isoformat() for day in (28, 29, 30)
    ] + [local(2026, 10, day, 7, 0).isoformat() for day in (1, 2)]
    assert events[0]["summary"] == "Wake up"
    assert events[0]["end"] == local(2026, 9, 28, 7, 30).isoformat()
    assert "Volume: 25%" in events[0]["description"]

    state = hass.states.get(entity_id)
    assert state.state == STATE_OFF
    assert state.attributes["message"] == "Wake up"
    assert state.attributes["start_time"] == "2026-09-28 07:00:00"

    await _set_schedule(hass, "wake", "Wake up", "07:00", days=[1, 2, 3, 4, 5], enabled=False)
    assert await _events(hass, entity_id, local(2026, 9, 27, 0, 0), local(2026, 10, 4, 0, 0)) == []
    assert "message" not in hass.states.get(entity_id).attributes


@B5
async def test_next_event_is_the_soonest_schedule(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """The calendar state shows the soonest schedule, whatever the storage order."""
    freezer.move_to(local(2026, 9, 28, 6, 0))
    await _set_schedule(hass, "late", "Evening", "21:00")
    await _set_schedule(hass, "early", "Morning", "08:00")
    await async_engine_updated(hass)
    assert hass.states.get(_calendar_id(hass, loaded_entry)).attributes["message"] == "Morning"


@B5
async def test_calendar_is_on_during_a_schedule_event(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """Ten minutes into a 30 minute schedule event the calendar is on."""
    freezer.move_to(local(2026, 9, 28, 7, 10))
    await _set_schedule(hass, "wake", "Wake up", "07:00")
    await async_engine_updated(hass)
    assert hass.states.get(_calendar_id(hass, loaded_entry)).state == STATE_ON


@B5
async def test_range_query_includes_an_event_in_progress(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """A range starting inside an event still returns that event."""
    freezer.move_to(local(2026, 9, 28, 6, 0))
    await _set_schedule(hass, "wake", "Wake up", "07:00")
    events = await _events(
        hass, _calendar_id(hass, loaded_entry), local(2026, 9, 28, 7, 10), local(2026, 9, 28, 8, 0)
    )
    assert [event["summary"] for event in events] == ["Wake up"]
