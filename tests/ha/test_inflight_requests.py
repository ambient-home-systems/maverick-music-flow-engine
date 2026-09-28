"""Single-flight requests: a cancelled caller never pins an old result or error (B-4, B-18).

Queue, library and radio directory reads share one in-flight request per key. When the
only caller is cancelled (the card disconnects, a timeout wrapper fires) the request keeps
running; it must free its slot when it finishes, so the next caller gets fresh data and
never a failed request's error.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientError
from conftest import KITCHEN, engine_runtime
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maverick_music_flow import radio_directory
from custom_components.maverick_music_flow.exceptions import HomeiiFlowServiceUnavailable


class ControlledFetch:
    """Fetch stub that can block until released and returns queued outcomes in order."""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        self.started.set()
        await self.release.wait()
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


async def _cancel_only_caller(
    hass: HomeAssistant, fetch: ControlledFetch, request: Callable[[], Awaitable[Any]]
) -> None:
    """Cancel the only caller mid-request, then let the request finish on its own."""
    fetch.release.clear()
    caller = asyncio.create_task(request())
    await asyncio.wait_for(fetch.started.wait(), 5)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    fetch.release.set()
    # The request is a tracked Home Assistant task, so this waits for it to finish.
    await hass.async_block_till_done()


# Queue


def _queue_answer(name: str) -> list[dict[str, Any]]:
    return [
        {
            "provider": "test",
            "normalized": {"items": [{"name": name}], "queue_id": "q"},
            "visible_items": 1,
        }
    ]


def _queue_names(result: dict[str, Any]) -> list[str]:
    return [item["name"] for item in result["items"]]


@pytest.fixture
def queue_fetch(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> ControlledFetch:
    fetch = ControlledFetch()
    engine_runtime(hass)._try_music_queue_command_bridge = fetch
    return fetch


async def _get_queue(hass: HomeAssistant) -> dict[str, Any]:
    return await engine_runtime(hass).async_get_queue({"entity_id": KITCHEN, "queue_id": "q"})


async def test_queue_after_cancelled_caller_gets_fresh_data(
    hass: HomeAssistant, queue_fetch: ControlledFetch
) -> None:
    """The finished request is cached as usual and its slot is freed."""
    runtime = engine_runtime(hass)
    queue_fetch.outcomes = [_queue_answer("first"), _queue_answer("second")]
    await _cancel_only_caller(hass, queue_fetch, lambda: _get_queue(hass))
    assert runtime._queue_inflight == {}

    # Straight after, the 2-second cache holds the result the finished request stored.
    cached = await _get_queue(hass)
    assert _queue_names(cached) == ["first"]
    assert cached["cache"]["hit"] is True
    assert len(queue_fetch.calls) == 1

    # Once the queue changes, the next request fetches instead of reusing the old task.
    runtime._bump_snapshot_revision("queue")
    fresh = await _get_queue(hass)
    assert _queue_names(fresh) == ["second"]
    assert len(queue_fetch.calls) == 2


async def test_queue_failed_fetch_is_not_reused(
    hass: HomeAssistant, queue_fetch: ControlledFetch
) -> None:
    """A request that failed after its only caller left is not served to the next one."""
    queue_fetch.outcomes = [HomeiiFlowServiceUnavailable("MA went away"), _queue_answer("ok")]
    await _cancel_only_caller(hass, queue_fetch, lambda: _get_queue(hass))
    assert engine_runtime(hass)._queue_inflight == {}

    assert _queue_names(await _get_queue(hass)) == ["ok"]
    assert len(queue_fetch.calls) == 2


async def test_queue_finished_task_in_slot_is_not_joined(
    hass: HomeAssistant, queue_fetch: ControlledFetch
) -> None:
    """A finished task whose done-callback has not run yet starts a new request."""
    runtime = engine_runtime(hass)
    failed: asyncio.Future[dict[str, Any]] = hass.loop.create_future()
    failed.set_exception(HomeiiFlowServiceUnavailable("old error"))
    failed.exception()
    runtime._queue_inflight[(KITCHEN, "q", 50, 450)] = failed
    queue_fetch.outcomes = [_queue_answer("new")]

    assert _queue_names(await _get_queue(hass)) == ["new"]
    assert len(queue_fetch.calls) == 1


# Library

LIBRARY_KEY = ("playlist", "", 50, False, "", 0)


def _library_answer(name: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[Any]]:
    items = [{"name": name, "uri": f"library://playlist/{name}"}]
    return {"items": items}, items, []


def _library_names(result: dict[str, Any]) -> list[str]:
    return [item["name"] for item in result["items"]]


@pytest.fixture
def library_fetch(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> ControlledFetch:
    runtime = engine_runtime(hass)
    runtime._library_cache.clear()
    fetch = ControlledFetch()
    runtime._try_music_library_command_bridge = fetch
    return fetch


async def _get_library(hass: HomeAssistant, limit: int = 50) -> dict[str, Any]:
    return await engine_runtime(hass).async_get_library({"media_type": "playlist", "limit": limit})


def _expire(entry: dict[str, Any], *, stale: bool) -> None:
    """Make a cache entry stale (still servable) or fully expired."""
    now = time.monotonic()
    entry["fresh_until"] = now - 1
    entry["stale_until"] = now + 3600 if stale else now - 1


async def test_library_expired_entry_after_cancelled_caller_fetches_again(
    hass: HomeAssistant, library_fetch: ControlledFetch
) -> None:
    """After stale_until the next caller fetches instead of getting the old task's result."""
    runtime = engine_runtime(hass)
    library_fetch.outcomes = [_library_answer("first"), _library_answer("second")]
    await _cancel_only_caller(hass, library_fetch, lambda: _get_library(hass))
    assert runtime._library_inflight == {}
    assert _library_names(runtime._library_cache[LIBRARY_KEY]["result"]) == ["first"]

    _expire(runtime._library_cache[LIBRARY_KEY], stale=False)
    fresh = await _get_library(hass)
    assert _library_names(fresh) == ["second"]
    assert fresh["cache"]["hit"] is False
    assert len(library_fetch.calls) == 2


async def test_library_failed_fetch_is_not_reused(
    hass: HomeAssistant, library_fetch: ControlledFetch
) -> None:
    """A library request that failed after its only caller left is not served again."""
    library_fetch.outcomes = [HomeiiFlowServiceUnavailable("MA went away"), _library_answer("ok")]
    await _cancel_only_caller(hass, library_fetch, lambda: _get_library(hass))
    assert engine_runtime(hass)._library_inflight == {}

    assert _library_names(await _get_library(hass)) == ["ok"]
    assert len(library_fetch.calls) == 2


async def test_library_stale_entry_triggers_a_refresh(
    hass: HomeAssistant, library_fetch: ControlledFetch
) -> None:
    """A stale entry is served at once and refreshed in the background, once."""
    runtime = engine_runtime(hass)
    library_fetch.outcomes = [_library_answer("first"), _library_answer("second")]
    # Even a cancelled first caller must not block the later refresh.
    await _cancel_only_caller(hass, library_fetch, lambda: _get_library(hass))
    _expire(runtime._library_cache[LIBRARY_KEY], stale=True)
    refreshes = runtime._media_cache_metrics["background_refreshes"]

    library_fetch.release.clear()
    stale = await _get_library(hass)
    assert _library_names(stale) == ["first"]
    assert stale["cache"]["stale"] is True
    # A second stale read while the refresh runs joins it instead of starting another.
    await _get_library(hass)
    library_fetch.release.set()
    await hass.async_block_till_done()

    assert len(library_fetch.calls) == 2
    assert runtime._media_cache_metrics["background_refreshes"] == refreshes + 1
    assert runtime._library_inflight == {}
    fresh = await _get_library(hass)
    assert _library_names(fresh) == ["second"]
    assert fresh["cache"]["hit"] is True
    assert fresh["cache"]["stale"] is False


async def test_library_stale_superset_refreshes_the_served_shelf(
    hass: HomeAssistant, library_fetch: ControlledFetch
) -> None:
    """A smaller read served from a larger stale shelf refreshes that larger shelf."""
    runtime = engine_runtime(hass)
    library_fetch.outcomes = [_library_answer("first"), _library_answer("second")]
    await _get_library(hass, limit=200)
    large_key = ("playlist", "", 200, False, "", 0)
    _expire(runtime._library_cache[large_key], stale=True)

    stale = await _get_library(hass, limit=50)
    assert stale["cache"]["source"] == "engine_stale_superset"
    await hass.async_block_till_done()

    assert library_fetch.calls[1]["limit"] == 200
    assert _library_names(runtime._library_cache[large_key]["result"]) == ["second"]
    assert LIBRARY_KEY not in runtime._library_cache


async def test_library_failed_refresh_is_counted_and_keeps_stale_data(
    hass: HomeAssistant, library_fetch: ControlledFetch
) -> None:
    runtime = engine_runtime(hass)
    library_fetch.outcomes = [_library_answer("first"), HomeiiFlowServiceUnavailable("down")]
    await _get_library(hass)
    _expire(runtime._library_cache[LIBRARY_KEY], stale=True)
    failures = runtime._media_cache_metrics["refresh_failures"]

    await _get_library(hass)
    await hass.async_block_till_done()

    assert runtime._media_cache_metrics["refresh_failures"] == failures + 1
    assert runtime._library_inflight == {}
    assert _library_names(await _get_library(hass)) == ["first"]


# Radio directory

STATIONS = [{"name": "Radio One", "url_resolved": "https://radio.test/one", "stationuuid": "s1"}]


class FakeRadioBrowser:
    """Stands in for the aiohttp session radio_directory uses for Radio Browser."""

    def __init__(self, fetch: ControlledFetch) -> None:
        self.fetch = fetch

    def get(self, _url: str, **kwargs: Any) -> contextlib.AbstractAsyncContextManager[Any]:
        @contextlib.asynccontextmanager
        async def request():
            data = await self.fetch(params=kwargs["params"])
            yield SimpleNamespace(raise_for_status=lambda: None, json=AsyncMock(return_value=data))

        return request()


@pytest.fixture
def radio_fetch(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
) -> ControlledFetch:
    fetch = ControlledFetch()
    session = FakeRadioBrowser(fetch)
    monkeypatch.setattr(radio_directory, "async_get_clientsession", lambda _hass: session)
    return fetch


async def _search(hass: HomeAssistant) -> list[dict[str, Any]]:
    return await radio_directory.search_stations(engine_runtime(hass), {"query": "one"})


async def test_radio_failed_search_is_not_reused(
    hass: HomeAssistant, radio_fetch: ControlledFetch
) -> None:
    """A search that failed after its only caller left is not served to the next one."""
    radio_fetch.outcomes = [ClientError("Radio Browser down"), STATIONS]
    await _cancel_only_caller(hass, radio_fetch, lambda: _search(hass))
    assert engine_runtime(hass)._radio_directory_pending == {}

    stations = await _search(hass)
    assert [station["name"] for station in stations] == ["Radio One"]
    assert len(radio_fetch.calls) == 2


async def test_radio_search_after_cancelled_caller_is_cached(
    hass: HomeAssistant, radio_fetch: ControlledFetch
) -> None:
    """A search that succeeded after its only caller left fills the cache and frees its slot."""
    runtime = engine_runtime(hass)
    radio_fetch.outcomes = [STATIONS]
    await _cancel_only_caller(hass, radio_fetch, lambda: _search(hass))
    assert runtime._radio_directory_pending == {}

    assert [station["name"] for station in await _search(hass)] == ["Radio One"]
    assert len(radio_fetch.calls) == 1


async def test_radio_concurrent_searches_share_one_request(
    hass: HomeAssistant, radio_fetch: ControlledFetch
) -> None:
    radio_fetch.outcomes = [STATIONS]
    radio_fetch.release.clear()
    first = asyncio.create_task(_search(hass))
    second = asyncio.create_task(_search(hass))
    await asyncio.wait_for(radio_fetch.started.wait(), 5)
    radio_fetch.release.set()

    assert await first == await second
    assert len(radio_fetch.calls) == 1
