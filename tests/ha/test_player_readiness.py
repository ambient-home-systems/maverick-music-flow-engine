"""Schedule player readiness follows Music Assistant player changes (B-9).

The Music Assistant player map used to be refreshed only by the startup probe, card
bootstrap and players/get. A speaker that was off when Home Assistant started kept failing
schedules, and a speaker that went offline was still treated as ready. The map now
refreshes on MA player events and, when it is stale, before a schedule readiness check;
while it stays stale the Home Assistant entity state decides.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from conftest import (
    KITCHEN,
    KITCHEN_ID,
    FakeCommandError,
    FakeMusicAssistant,
    async_wait_for,
    engine_runtime,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maverick_music_flow import runtime as runtime_module
from custom_components.maverick_music_flow.const import DOMAIN


@pytest.fixture(autouse=True)
def fast_timings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refresh on player events at once and make play confirmation instant."""
    monkeypatch.setattr(runtime_module, "_MA_PLAYERS_EVENT_REFRESH_DELAY", 0)
    monkeypatch.setattr(runtime_module, "_PLAY_VERIFY_POLLS", 2)
    monkeypatch.setattr(runtime_module, "_PLAY_VERIFY_INTERVAL", 0)


@pytest.fixture
def kitchen_off_at_startup(fake_ma: FakeMusicAssistant) -> None:
    """Make Music Assistant report the kitchen speaker unavailable when the Engine starts.

    Request it before ``loaded_entry`` so the startup probe sees it.
    """
    _set_kitchen_available(fake_ma, False)


def _set_kitchen_available(fake_ma: FakeMusicAssistant, available: bool) -> None:
    for player in fake_ma.players:
        if player["player_id"] == KITCHEN_ID:
            player["available"] = available


def _age_player_data(hass: HomeAssistant, seconds: float = 1000) -> None:
    """Make the Engine's Music Assistant player data older than the refresh limit."""
    runtime = engine_runtime(hass)
    runtime._ma_players_refreshed_at -= seconds


async def _set_schedule(hass: HomeAssistant) -> None:
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
        },
        blocking=True,
    )


async def _fire_schedule(hass: HomeAssistant) -> dict[str, Any]:
    """Fire the schedule the way its 07:00 timer does."""
    return await engine_runtime(hass).async_run_scheduled_schedule("default", "wake")


async def test_player_off_at_startup_is_ready_after_player_event(
    hass: HomeAssistant,
    kitchen_off_at_startup: None,
    loaded_entry: MockConfigEntry,
    fake_ma: FakeMusicAssistant,
) -> None:
    """A player_updated event refreshes the player map, so the schedule plays."""
    runtime = engine_runtime(hass)
    readiness = runtime._player_readiness(KITCHEN)
    assert readiness["ready"] is False
    assert readiness["source"] == "music_assistant"
    assert readiness["reason"].startswith("Music Assistant player is unavailable (")
    assert "player data is" in readiness["reason"]

    _set_kitchen_available(fake_ma, True)
    await fake_ma.async_send_event("player_updated", KITCHEN_ID)
    await async_wait_for(
        lambda: runtime._player_readiness(KITCHEN)["ready"],
        message="the player_updated event to refresh the player map",
    )
    await hass.async_block_till_done()
    await _set_schedule(hass)

    result = await _fire_schedule(hass)

    assert result["ok"] is True
    assert result["availability_attempts"][0]["readiness"]["ready"] is True
    assert len(fake_ma.commands_named("player_queues/play_media")) == 1


async def test_player_off_at_startup_is_ready_when_stale_data_is_refreshed(
    hass: HomeAssistant,
    kitchen_off_at_startup: None,
    loaded_entry: MockConfigEntry,
    fake_ma: FakeMusicAssistant,
) -> None:
    """Without an event, the schedule refreshes stale player data before giving up."""
    _set_kitchen_available(fake_ma, True)
    _age_player_data(hass)
    await _set_schedule(hass)
    refreshes = len(fake_ma.commands_named("players/all"))

    result = await _fire_schedule(hass)

    assert result["ok"] is True
    assert len(fake_ma.commands_named("players/all")) > refreshes
    assert len(fake_ma.commands_named("player_queues/play_media")) == 1


async def test_player_going_offline_makes_readiness_false(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, fake_ma: FakeMusicAssistant
) -> None:
    """A player_updated event for a player that went offline makes it not ready."""
    runtime = engine_runtime(hass)
    assert runtime._player_readiness(KITCHEN)["ready"] is True

    _set_kitchen_available(fake_ma, False)
    await fake_ma.async_send_event("player_updated", KITCHEN_ID)
    await async_wait_for(
        lambda: not runtime._player_readiness(KITCHEN)["ready"],
        message="the player_updated event to refresh the player map",
    )

    readiness = await runtime.async_player_readiness(KITCHEN)
    assert readiness["ready"] is False
    assert readiness["reason"].startswith("Music Assistant player is unavailable (")


async def test_removed_player_leaves_the_player_map(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, fake_ma: FakeMusicAssistant
) -> None:
    """player_removed refreshes the map, so readiness no longer uses the old entry."""
    runtime = engine_runtime(hass)
    fake_ma.players = [p for p in fake_ma.players if p["player_id"] != KITCHEN_ID]

    await fake_ma.async_send_event("player_removed", KITCHEN_ID)
    await async_wait_for(
        lambda: KITCHEN not in runtime._ma_players_by_entity,
        message="the player_removed event to refresh the player map",
    )

    readiness = runtime._player_readiness(KITCHEN)
    assert readiness["source"] == "home_assistant"


async def test_offline_player_with_stale_data_falls_back_to_ha_state(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, fake_ma: FakeMusicAssistant
) -> None:
    """Stale data saying "available" does not win over an unavailable HA entity."""
    runtime = engine_runtime(hass)

    def unreachable(_args: dict[str, Any]) -> Any:
        raise FakeCommandError("Music Assistant is restarting")

    fake_ma.responses["players/all"] = unreachable
    _age_player_data(hass, 300)
    hass.states.async_set(KITCHEN, "unavailable")

    readiness = await runtime.async_player_readiness(KITCHEN)

    assert readiness["ready"] is False
    assert readiness["source"] == "home_assistant"
    assert readiness["ma_data_stale"] is True
    assert readiness["ma_data_age"] >= 300
    assert readiness["reason"].startswith("player is unavailable (Music Assistant player data is")
    assert readiness["reason"].endswith("s old)")


async def test_stale_unavailable_data_falls_back_to_ha_state(
    hass: HomeAssistant,
    kitchen_off_at_startup: None,
    loaded_entry: MockConfigEntry,
    fake_ma: FakeMusicAssistant,
) -> None:
    """When MA cannot be refreshed, a stale "unavailable" does not block a fine player."""

    def unreachable(_args: dict[str, Any]) -> Any:
        raise FakeCommandError("Music Assistant is restarting")

    fake_ma.responses["players/all"] = unreachable
    _age_player_data(hass)

    readiness = await engine_runtime(hass).async_player_readiness(KITCHEN)

    assert readiness["ready"] is True
    assert readiness["source"] == "home_assistant"
    assert readiness["ma_data_stale"] is True


async def test_stale_data_is_refreshed_once_for_concurrent_checks(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, fake_ma: FakeMusicAssistant
) -> None:
    """Concurrent readiness checks share one players/all refresh."""
    runtime = engine_runtime(hass)
    _age_player_data(hass)
    refreshes = len(fake_ma.commands_named("players/all"))

    results = await asyncio.gather(*(runtime.async_player_readiness(KITCHEN) for _ in range(5)))

    assert all(result["ready"] for result in results)
    assert all(result["ma_data_stale"] is False for result in results)
    assert len(fake_ma.commands_named("players/all")) == refreshes + 1


async def test_fresh_data_is_not_refreshed(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, fake_ma: FakeMusicAssistant
) -> None:
    """A readiness check with fresh player data sends no players/all command."""
    refreshes = len(fake_ma.commands_named("players/all"))

    readiness = await engine_runtime(hass).async_player_readiness(KITCHEN)

    assert readiness["ready"] is True
    assert readiness["ma_data_stale"] is False
    assert len(fake_ma.commands_named("players/all")) == refreshes
