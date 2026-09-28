"""Calendar platform: schedules as calendar events (finding B-5)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from conftest import KITCHEN, async_engine_updated, engine_entity_id
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.calendar import DOMAIN as CALENDAR_DOMAIN
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maverick_music_flow.const import DOMAIN


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


async def test_next_event_is_the_soonest_schedule(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """The calendar state shows the soonest schedule, whatever the storage order."""
    freezer.move_to(local(2026, 9, 28, 6, 0))
    await _set_schedule(hass, "late", "Evening", "21:00")
    await _set_schedule(hass, "early", "Morning", "08:00")
    await async_engine_updated(hass)
    assert hass.states.get(_calendar_id(hass, loaded_entry)).attributes["message"] == "Morning"


async def test_calendar_is_on_during_a_schedule_event(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """Ten minutes into a 30 minute schedule event the calendar is on."""
    freezer.move_to(local(2026, 9, 28, 7, 10))
    await _set_schedule(hass, "wake", "Wake up", "07:00")
    await async_engine_updated(hass)
    assert hass.states.get(_calendar_id(hass, loaded_entry)).state == STATE_ON


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


async def test_calendar_turns_off_when_the_event_ends(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """The event is on from its start until its end, and off at the end minute."""
    freezer.move_to(local(2026, 9, 28, 7, 0))
    await _set_schedule(hass, "wake", "Wake up", "07:00")
    await async_engine_updated(hass)
    entity_id = _calendar_id(hass, loaded_entry)
    assert hass.states.get(entity_id).state == STATE_ON

    freezer.move_to(local(2026, 9, 28, 7, 30))
    await async_engine_updated(hass)
    assert hass.states.get(entity_id).state == STATE_OFF


async def test_range_query_excludes_events_that_do_not_overlap(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """A range that ends at the start, or starts at the end, of an event misses it."""
    freezer.move_to(local(2026, 9, 28, 6, 0))
    await _set_schedule(hass, "wake", "Wake up", "07:00")
    entity_id = _calendar_id(hass, loaded_entry)
    assert await _events(hass, entity_id, local(2026, 9, 28, 6, 0), local(2026, 9, 28, 7, 0)) == []
    assert await _events(hass, entity_id, local(2026, 9, 28, 7, 30), local(2026, 9, 28, 9, 0)) == []


async def test_range_query_includes_an_event_that_started_yesterday(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """An event running across midnight is returned for a range starting after midnight."""
    freezer.move_to(local(2026, 9, 28, 12, 0))
    await _set_schedule(hass, "late", "Late night", "23:50")
    events = await _events(
        hass, _calendar_id(hass, loaded_entry), local(2026, 9, 29, 0, 5), local(2026, 9, 29, 1, 0)
    )
    assert [event["start"] for event in events] == [local(2026, 9, 28, 23, 50).isoformat()]


async def test_limit_keeps_the_soonest_events_of_all_schedules(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """The limit applies after sorting, so a later stored schedule can still be first."""
    freezer.move_to(local(2026, 9, 28, 6, 0))
    await _set_schedule(hass, "late", "Evening", "22:00")
    await _set_schedule(hass, "early", "Morning", "07:00")
    calendar = hass.data["entity_components"][CALENDAR_DOMAIN].get_entity(
        _calendar_id(hass, loaded_entry)
    )
    events = calendar._events_between(local(2026, 9, 28, 6, 0), local(2026, 9, 30, 6, 0), limit=3)
    assert [(event.summary, event.start.hour) for event in events] == [
        ("Morning", 7),
        ("Evening", 22),
        ("Morning", 7),
    ]


async def test_event_in_the_repeated_hour_is_valid(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """A 01:50 event on the fall-back day (01:00 to 02:00 happens twice) ends after it starts."""
    freezer.move_to(local(2026, 11, 1, 0, 0))  # Clocks go back at 02:00 PDT.
    await _set_schedule(hass, "night", "Night", "01:50")
    events = await _events(
        hass, _calendar_id(hass, loaded_entry), local(2026, 11, 1, 0, 0), local(2026, 11, 2, 0, 0)
    )
    assert len(events) == 1
    start = dt_util.parse_datetime(events[0]["start"])
    end = dt_util.parse_datetime(events[0]["end"])
    assert start.astimezone(dt_util.UTC) < end.astimezone(dt_util.UTC)
