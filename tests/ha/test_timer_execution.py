"""Timer execution: timers set or deleted while a due timer runs are kept as set (B-8).

Running a due timer awaits a blocking media_stop and an activity save. Timers set or
deleted during those awaits must not be lost or brought back when the executed timer is
removed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import BEDROOM, KITCHEN, engine_runtime
from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maverick_music_flow.const import DOMAIN


class BlockingStop:
    """media_player.media_stop handler that waits until the test releases it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[str] = []

    async def handle(self, call: ServiceCall) -> None:
        self.calls.append(call.data["entity_id"])
        self.started.set()
        await self.release.wait()


@pytest.fixture
async def blocking_stop(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> AsyncGenerator[BlockingStop]:
    """Replace the mocked media_stop with one that blocks until released."""
    stop = BlockingStop()
    hass.services.async_register("media_player", "media_stop", stop.handle)
    yield stop
    stop.release.set()


async def _set_timer(hass: HomeAssistant, **data: Any) -> None:
    # No async_block_till_done: it would wait for the blocked timer run.
    await hass.services.async_call(DOMAIN, "set_timer", data, blocking=True)


async def _start_due_run(hass: HomeAssistant, stop: BlockingStop) -> asyncio.Task:
    """Run due timers 10 minutes from now and wait until media_stop is blocking."""
    run = hass.async_create_task(
        engine_runtime(hass).async_run_due_timers(datetime.now(UTC) + timedelta(minutes=10))
    )
    await asyncio.wait_for(stop.started.wait(), 5)
    return run


def _timer_ids(hass: HomeAssistant) -> list[str]:
    return [timer["id"] for timer in engine_runtime(hass).timers("default")]


async def test_timer_set_during_execution_is_kept(
    hass: HomeAssistant, blocking_stop: BlockingStop
) -> None:
    """A new timer set while a due timer runs survives; the executed one is removed."""
    await _set_timer(hass, id="nap", player=KITCHEN, minutes=5)
    run = await _start_due_run(hass, blocking_stop)

    await _set_timer(hass, id="later", player=BEDROOM, minutes=60)
    blocking_stop.release.set()
    results = await run
    await hass.async_block_till_done()

    assert [result["timer_id"] for result in results] == ["nap"]
    assert results[0]["ok"] is True
    assert blocking_stop.calls == [KITCHEN]
    assert _timer_ids(hass) == ["later"]


async def test_timer_reset_under_same_id_during_execution_is_kept(
    hass: HomeAssistant, blocking_stop: BlockingStop
) -> None:
    """Re-setting the running timer's id for a new end time keeps the new timer."""
    await _set_timer(hass, id="nap", player=KITCHEN, minutes=5)
    run = await _start_due_run(hass, blocking_stop)

    await _set_timer(hass, id="nap", player=KITCHEN, minutes=60)
    new_ends_at = engine_runtime(hass).timers("default")[0]["ends_at"]
    blocking_stop.release.set()
    await run
    await hass.async_block_till_done()

    timers = engine_runtime(hass).timers("default")
    assert [(timer["id"], timer["ends_at"]) for timer in timers] == [("nap", new_ends_at)]


async def test_timer_deleted_during_execution_stays_deleted(
    hass: HomeAssistant, blocking_stop: BlockingStop
) -> None:
    """A timer deleted while a due timer runs is not brought back."""
    await _set_timer(hass, id="nap", player=KITCHEN, minutes=5)
    await _set_timer(hass, id="later", player=BEDROOM, minutes=60)
    run = await _start_due_run(hass, blocking_stop)

    await hass.services.async_call(DOMAIN, "delete_timer", {"id": "later"}, blocking=True)
    blocking_stop.release.set()
    await run
    await hass.async_block_till_done()

    assert _timer_ids(hass) == []
