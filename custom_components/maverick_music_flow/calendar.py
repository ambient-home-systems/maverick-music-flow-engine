"""Calendar entities for Maverick Music Flow Engine schedules."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import async_get_runtime
from .const import (
    CONF_INSTANCE_ID,
    CONF_PROFILE_ID,
    DEFAULT_INSTANCE_ID,
    DEFAULT_PROFILE_ID,
    DOMAIN,
    NAME,
    SIGNAL_ENGINE_UPDATED,
    VERSION,
)
from .runtime import (
    HomeiiFlowRuntime,
    _homeii_weekday,
    _instant,
    _parse_hhmm,
    _schedule_days,
    _schedule_local_datetime,
)

# How long a schedule occurrence stays "on" in the calendar. A schedule fires at one
# moment; this window lets Home Assistant show the calendar as "on" while it plays.
SCHEDULE_EVENT_DURATION = timedelta(minutes=30)


def _profile_id(entry: ConfigEntry) -> str:
    """Return the active profile id for a config entry."""
    return str(
        entry.options.get(CONF_PROFILE_ID) or entry.data.get(CONF_PROFILE_ID) or DEFAULT_PROFILE_ID
    )


def _as_local_datetime(value: date | datetime) -> datetime:
    """Convert a date or datetime into a local timezone-aware datetime.

    A plain date means the start of that local day, which is a valid instant even when
    daylight saving skips midnight.
    """
    if isinstance(value, datetime):
        return dt_util.as_local(value)
    return dt_util.start_of_local_day(value)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Maverick Music Flow Engine schedule calendar."""
    runtime = async_get_runtime(hass)
    async_add_entities([HomeiiFlowScheduleCalendar(runtime, entry)])


class HomeiiFlowScheduleCalendar(CalendarEntity):
    """Expose Maverick schedules as a Home Assistant calendar."""

    _attr_has_entity_name = True
    _attr_name = "Schedules calendar"
    _attr_icon = "mdi:calendar-music"

    def __init__(self, runtime: HomeiiFlowRuntime, entry: ConfigEntry) -> None:
        """Initialize the schedule calendar."""
        self._runtime = runtime
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_schedules_calendar"

    async def async_added_to_hass(self) -> None:
        """Subscribe to Engine updates."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_ENGINE_UPDATED, self.async_write_ha_state)
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        instance_id = str(self._entry.data.get(CONF_INSTANCE_ID) or DEFAULT_INSTANCE_ID)
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title or NAME,
            manufacturer="Maverick",
            model="Music Flow Engine",
            sw_version=VERSION,
            configuration_url="https://github.com/ambient-home-systems/maverick-music-flow-engine",
            suggested_area=instance_id if instance_id != DEFAULT_INSTANCE_ID else None,
        )

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming schedule event."""
        now = dt_util.now()
        events = self._events_between(now, now + timedelta(days=8), limit=1)
        return events[0] if events else None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: date | datetime,
        end_date: date | datetime,
    ) -> list[CalendarEvent]:
        """Return schedule events in the requested range."""
        return self._events_between(start_date, end_date)

    def _events_between(
        self,
        start_date: date | datetime,
        end_date: date | datetime,
        *,
        limit: int = 250,
    ) -> list[CalendarEvent]:
        """Return the soonest events that overlap the range, from every schedule.

        A range that is a plain end date includes that whole day. Candidates from all
        schedules are collected and sorted before the limit is applied, so the limit
        keeps the soonest events rather than those of the first stored schedules.
        """
        profile_id = _profile_id(self._entry)
        start = _as_local_datetime(start_date)
        if isinstance(end_date, datetime):
            end = _as_local_datetime(end_date)
        else:
            end = _as_local_datetime(end_date + timedelta(days=1))
        if _instant(end) <= _instant(start):
            return []

        events: list[CalendarEvent] = []
        for schedule in self._runtime.schedules(profile_id):
            events.extend(self._schedule_events(schedule, start, end, limit))
        events.sort(key=lambda event: _instant(event.start))
        return events[:limit]

    def _schedule_events(
        self,
        schedule: dict[str, Any],
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[CalendarEvent]:
        """Return up to ``limit`` events of one schedule that overlap ``start`` to ``end``.

        An event overlaps when it ends after the range starts and starts before the range
        ends, so an event already in progress at ``start`` is included.
        """
        if limit <= 0 or not bool(schedule.get("enabled", True)):
            return []
        parsed_time = _parse_hhmm(schedule.get("time"))
        if parsed_time is None:
            return []
        days = _schedule_days(schedule)
        zone = dt_util.get_default_time_zone()
        events: list[CalendarEvent] = []
        # Begin a day early: an event that started before midnight may still be running.
        day = start.date() - timedelta(days=1)
        end_day = end.date()
        while day <= end_day and len(events) < limit:
            if not days or _homeii_weekday(day) in days:
                event_start = _schedule_local_datetime(day, *parsed_time, zone)
                event_end = dt_util.as_local(_instant(event_start) + SCHEDULE_EVENT_DURATION)
                if event_end.replace(tzinfo=None) <= event_start.replace(tzinfo=None):
                    # The clocks went back during the event. Home Assistant validates
                    # and compares times in one zone by wall clock and would reject an
                    # end before the start, so fall back to the wall-clock end (the
                    # event then spans the repeated hour).
                    event_end = event_start + SCHEDULE_EVENT_DURATION
                if _instant(event_end) > _instant(start) and _instant(event_start) < _instant(end):
                    events.append(self._calendar_event(schedule, event_start, event_end))
            day += timedelta(days=1)
        return events

    def _calendar_event(
        self, schedule: dict[str, Any], event_start: datetime, event_end: datetime
    ) -> CalendarEvent:
        """Build one Home Assistant calendar event."""
        name = str(
            schedule.get("name")
            or schedule.get("media_name")
            or schedule.get("playlist_name")
            or "Maverick schedule"
        )
        media_name = str(schedule.get("media_name") or schedule.get("playlist_name") or "").strip()
        player = str(schedule.get("player") or "").strip()
        volume = schedule.get("volume")
        details = [
            f"Player: {player}" if player else "",
            f"Media: {media_name}" if media_name else "",
            f"Volume: {volume}%" if volume not in (None, "") else "",
            f"Mode: {schedule.get('media_mode')}" if schedule.get("media_mode") else "",
        ]
        return CalendarEvent(
            summary=name,
            start=event_start,
            end=event_end,
            description="\n".join(detail for detail in details if detail),
            uid=f"maverick-music-flow-{schedule.get('id') or schedule.get('schedule_id')}-{event_start.isoformat()}",
        )
