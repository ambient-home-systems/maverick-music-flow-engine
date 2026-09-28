"""Volume rule time windows (B-6).

A window includes its start minute and excludes its end minute. A window whose start is
later than its end crosses midnight, and the part after midnight belongs to the day the
window started: the rule's days are checked against that day, not the current one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from conftest import BEDROOM, MusicAssistantStub, engine_runtime
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maverick_music_flow.const import DOMAIN
from custom_components.maverick_music_flow.runtime import _time_window_active

FRIDAY = 5  # Day indexes start at Sunday = 0.


def local(*args: int) -> datetime:
    """Return a datetime in Home Assistant's time zone (US/Pacific in tests)."""
    return datetime(*args, tzinfo=dt_util.get_default_time_zone())


@pytest.fixture
def quiet_clock(loaded_entry: MockConfigEntry, freezer: FrozenDateTimeFactory) -> None:
    """Freeze the clock outside every window below (a Wednesday evening).

    Saving a rule and the Engine's own ticks enforce rules at the current time.
    """
    freezer.move_to(local(2026, 9, 30, 20, 0))


async def _limited_at(
    hass: HomeAssistant, stub: MusicAssistantStub, when: tuple[int, ...], **rule: Any
) -> bool:
    """Store a 25% bedroom rule, enforce it at local time ``when``, return if it applied."""
    await hass.services.async_call(
        DOMAIN,
        "set_volume_rule",
        {"player": BEDROOM, "max_volume": 25, **rule},
        blocking=True,
    )
    calls = stub.service_calls["media_player.volume_set"]
    calls.clear()
    await engine_runtime(hass).async_enforce_volume_rules(local(*when))
    assert [call.data for call in calls] in (
        [],
        [{"entity_id": BEDROOM, "volume_level": 0.25}],
    )
    return bool(calls)


@pytest.mark.parametrize(
    ("when", "limited"),
    [
        pytest.param((2026, 10, 2, 21, 59), False, id="friday-before-start"),
        pytest.param((2026, 10, 2, 22, 0), True, id="friday-start-minute"),
        pytest.param((2026, 10, 2, 23, 0), True, id="friday-23:00"),
        pytest.param((2026, 10, 3, 5, 59), True, id="saturday-05:59"),
        pytest.param((2026, 10, 3, 6, 0), False, id="saturday-end-minute"),
        pytest.param((2026, 10, 2, 5, 0), False, id="friday-05:00-thursday-window"),
        pytest.param((2026, 10, 3, 22, 30), False, id="saturday-22:30"),
    ],
)
@pytest.mark.usefixtures("quiet_clock")
async def test_overnight_rule_applies_from_the_day_it_starts(
    hass: HomeAssistant,
    mock_music_assistant: MusicAssistantStub,
    when: tuple[int, ...],
    limited: bool,
) -> None:
    """A Friday-only 22:00-06:00 limit runs from Friday night into Saturday morning."""
    assert (
        await _limited_at(
            hass,
            mock_music_assistant,
            when,
            start_time="22:00",
            end_time="06:00",
            days=[FRIDAY],
        )
        is limited
    )


@pytest.mark.parametrize(
    ("start_time", "end_time", "when", "limited"),
    [
        ("22:00", "07:00", (2026, 9, 30, 6, 59, 59), True),
        ("22:00", "07:00", (2026, 9, 30, 7, 0), False),
        ("22:00", "07:00", (2026, 9, 30, 7, 0, 59), False),
        ("09:00", "17:00", (2026, 9, 30, 16, 59, 59), True),
        ("09:00", "17:00", (2026, 9, 30, 17, 0), False),
    ],
)
@pytest.mark.usefixtures("quiet_clock")
async def test_end_minute_is_exclusive(
    hass: HomeAssistant,
    mock_music_assistant: MusicAssistantStub,
    start_time: str,
    end_time: str,
    when: tuple[int, ...],
    limited: bool,
) -> None:
    """A window ending at 07:00 stops at 07:00, not at 07:00:59."""
    assert (
        await _limited_at(
            hass, mock_music_assistant, when, start_time=start_time, end_time=end_time
        )
        is limited
    )


@pytest.mark.parametrize(
    ("when", "limited"),
    [
        ((2026, 10, 2, 21, 59), False),
        ((2026, 10, 2, 22, 0), True),
        ((2026, 10, 3, 21, 59), True),
        ((2026, 10, 3, 22, 0), False),
    ],
)
@pytest.mark.usefixtures("quiet_clock")
async def test_equal_start_and_end_cover_24_hours(
    hass: HomeAssistant,
    mock_music_assistant: MusicAssistantStub,
    when: tuple[int, ...],
    limited: bool,
) -> None:
    """A Friday-only 22:00-22:00 limit lasts from Friday 22:00 to Saturday 22:00."""
    assert (
        await _limited_at(
            hass,
            mock_music_assistant,
            when,
            start_time="22:00",
            end_time="22:00",
            days=[FRIDAY],
        )
        is limited
    )


@pytest.mark.parametrize(
    ("start_time", "end_time", "when", "active"),
    [
        ("", "", (2026, 9, 30, 12, 0), True),
        ("22:00", "", (2026, 9, 30, 23, 59), True),
        ("22:00", "", (2026, 10, 1, 0, 0), False),
        ("", "07:00", (2026, 9, 30, 6, 59), True),
        ("", "07:00", (2026, 9, 30, 7, 0), False),
    ],
)
def test_missing_start_or_end_means_midnight(
    start_time: str, end_time: str, when: tuple[int, ...], active: bool
) -> None:
    """With no start the window opens at midnight; with no end it closes at midnight."""
    assert _time_window_active(local(*when), start_time, end_time) is active


def test_day_after_an_overnight_window_is_checked_against_the_previous_day() -> None:
    """Sunday 00:00-06:00 belongs to a window that starts on Saturday (day 6)."""
    sunday_morning = local(2026, 10, 4, 3, 0)
    assert _time_window_active(sunday_morning, "22:00", "06:00", [6]) is True
    assert _time_window_active(sunday_morning, "22:00", "06:00", [0]) is False
