"""Run the real setup, unload and reload code against small Home Assistant stand-ins.

Home Assistant is not installed for these tests. The integration package (__init__.py,
runtime.py, artwork_lighting.py, ma_client.py, websocket_api.py and the rest) is imported
with stand-ins for the Home Assistant and aiohttp modules it uses. The stand-in event
helpers record every timer and listener, so the tests can check that unloading the last
config entry removes all of them and that a reload creates exactly one set again.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import logging
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/maverick_music_flow"
PACKAGE = "_mmf_lifecycle_under_test"
DOMAIN = "maverick_music_flow"
KITCHEN = "media_player.kitchen"
MA_URL = "http://ma.test:8095"


class HTTPException(Exception):
    def __init__(self, *, text: str = "") -> None:
        super().__init__(text)
        self.text = text


class HTTPNotFound(HTTPException):
    pass


class HTTPServiceUnavailable(HTTPException):
    pass


class ClientError(Exception):
    pass


class ServiceValidationError(Exception):
    def __init__(self, message: str = "", **kwargs: Any) -> None:
        super().__init__(message)
        self.kwargs = kwargs


class Handles:
    """Stand-in for Home Assistant's timers and listeners that remembers each one."""

    def __init__(self) -> None:
        self.all: list[SimpleNamespace] = []
        self.double_removals = 0

    def add(self, kind: str, action: Any, *, once: bool = False) -> Any:
        handle = SimpleNamespace(kind=kind, action=action, once=once, active=True)
        self.all.append(handle)

        def remove() -> None:
            # Home Assistant logs an error when a listener that already fired is removed.
            if not handle.active and handle.kind.startswith("bus:"):
                self.double_removals += 1
            handle.active = False

        return remove

    def active(self, kind: str | None = None) -> list[SimpleNamespace]:
        return [h for h in self.all if h.active and (kind is None or h.kind == kind)]

    def counts(self) -> dict[str, int]:
        found: dict[str, int] = {}
        for handle in self.active():
            found[handle.kind] = found.get(handle.kind, 0) + 1
        return found

    def fire(self, kind: str, *args: Any) -> int:
        fired = 0
        for handle in self.active(kind):
            if handle.once:
                handle.active = False
            handle.action(*args)
            fired += 1
        return fired


HANDLES = Handles()


def _call_later(hass, delay, action):
    return HANDLES.add("call_later", action, once=True)


def _point_in_time(hass, action, when):
    return HANDLES.add("point_in_time", action, once=True)


def _state_change(hass, entity_ids, action):
    return HANDLES.add("state_change", action)


def _time_change(hass, action, **kwargs):
    return HANDLES.add("time_change", action)


def _time_interval(hass, action, interval):
    return HANDLES.add(f"interval:{int(interval.total_seconds())}", action)


class Store:
    def __init__(self, hass, version, key) -> None:
        self.hass = hass
        self.key = key

    async def async_load(self):
        return copy.deepcopy(self.hass.stored.get(self.key))

    async def async_save(self, data) -> None:
        self.hass.stored[self.key] = copy.deepcopy(data)
        self.hass.saves[self.key] = self.hass.saves.get(self.key, 0) + 1


def _module(name: str, **attrs: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


def _passthrough(func):
    return func


SCHEMA_CALLS: list[str] = []
WS_COMMANDS: list[Any] = []

_STUBS = {
    "aiohttp": _module(
        "aiohttp",
        web=SimpleNamespace(
            Request=object,
            Response=object,
            WebSocketResponse=object,
            HTTPException=HTTPException,
            HTTPNotFound=HTTPNotFound,
            HTTPServiceUnavailable=HTTPServiceUnavailable,
            HTTPBadRequest=HTTPException,
            HTTPForbidden=HTTPException,
            HTTPBadGateway=HTTPException,
            HTTPTooManyRequests=HTTPException,
            json_response=lambda result: {"json": result},
        ),
        ClientError=ClientError,
        ClientTimeout=lambda **kwargs: kwargs,
        ClientSession=object,
        ClientWebSocketResponse=object,
        WSMsgType=SimpleNamespace(TEXT=1, BINARY=2, ERROR=3, CLOSE=4, CLOSED=5, CLOSING=6),
        WSCloseCode=SimpleNamespace(OK=1000, GOING_AWAY=1001, POLICY_VIOLATION=1008),
    ),
    "aiohttp.abc": _module("aiohttp.abc", AbstractResolver=object),
    "homeassistant": _module("homeassistant"),
    "homeassistant.const": _module("homeassistant.const", EVENT_HOMEASSISTANT_CLOSE="homeassistant_close"),
    "homeassistant.core": _module(
        "homeassistant.core", HomeAssistant=object, ServiceCall=object, callback=_passthrough
    ),
    "homeassistant.config_entries": _module("homeassistant.config_entries", ConfigEntry=object),
    "homeassistant.exceptions": _module(
        "homeassistant.exceptions",
        HomeAssistantError=Exception,
        ServiceValidationError=ServiceValidationError,
        Unauthorized=PermissionError,
        UnknownUser=PermissionError,
    ),
    "homeassistant.auth": _module("homeassistant.auth"),
    "homeassistant.auth.permissions": _module("homeassistant.auth.permissions"),
    "homeassistant.auth.permissions.const": _module(
        "homeassistant.auth.permissions.const", POLICY_CONTROL="control"
    ),
    "homeassistant.components": _module("homeassistant.components"),
    "homeassistant.components.http": _module(
        "homeassistant.components.http",
        HomeAssistantView=object,
        StaticPathConfig=lambda *args, **kwargs: (args, kwargs),
    ),
    "homeassistant.components.websocket_api": _module(
        "homeassistant.components.websocket_api",
        websocket_command=lambda schema: _passthrough,
        async_response=_passthrough,
        ActiveConnection=object,
        ERR_UNAUTHORIZED="unauthorized",
        async_register_command=lambda hass, handler: WS_COMMANDS.append(handler),
    ),
    "homeassistant.components.tts": _module("homeassistant.components.tts"),
    "homeassistant.components.media_player": _module(
        "homeassistant.components.media_player", DATA_COMPONENT="media_player"
    ),
    "homeassistant.helpers": _module("homeassistant.helpers"),
    "homeassistant.helpers.config_validation": _module(
        "homeassistant.helpers.config_validation",
        config_entry_only_config_schema=lambda domain: SCHEMA_CALLS.append(domain) or "config-entry-only",
    ),
    "homeassistant.helpers.aiohttp_client": _module(
        "homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: hass.session
    ),
    "homeassistant.helpers.typing": _module("homeassistant.helpers.typing", ConfigType=dict),
    "homeassistant.helpers.service": _module(
        "homeassistant.helpers.service",
        async_register_admin_service=lambda hass, domain, service, handler, schema=None: (
            hass.services.async_register(domain, service, handler, schema=schema)
        ),
    ),
    "homeassistant.helpers.dispatcher": _module(
        "homeassistant.helpers.dispatcher", async_dispatcher_send=lambda hass, signal, *args: None
    ),
    "homeassistant.helpers.entity_registry": _module("homeassistant.helpers.entity_registry"),
    "homeassistant.helpers.event": _module(
        "homeassistant.helpers.event",
        async_call_later=_call_later,
        async_track_point_in_time=_point_in_time,
        async_track_state_change_event=_state_change,
        async_track_time_change=_time_change,
        async_track_time_interval=_time_interval,
    ),
    "homeassistant.helpers.storage": _module("homeassistant.helpers.storage", Store=Store),
    "homeassistant.helpers.network": _module(
        "homeassistant.helpers.network", NoURLAvailableError=Exception, get_url=lambda hass, **kw: ""
    ),
    "homeassistant.util": _module("homeassistant.util"),
    "homeassistant.util.dt": _module(
        "homeassistant.util.dt",
        now=lambda: datetime.now(UTC),
        as_local=lambda value: value.astimezone(UTC),
    ),
    "homeassistant.util.ssl": _module("homeassistant.util.ssl", client_context=lambda: None),
}


def load_package() -> types.ModuleType:
    """Import the integration package with Home Assistant and aiohttp stand-ins."""
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    installed = [name for name in _STUBS if name not in sys.modules]
    sys.modules.update({name: _STUBS[name] for name in installed})
    try:
        spec = importlib.util.spec_from_file_location(
            PACKAGE, COMPONENT / "__init__.py", submodule_search_locations=[str(COMPONENT)]
        )
        package = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE] = package
        spec.loader.exec_module(package)
    finally:
        for name in installed:
            sys.modules.pop(name, None)
    return package


ENGINE = load_package()
RUNTIME_MODULE = sys.modules[f"{PACKAGE}.runtime"]
# The stand-in Music Assistant is unreachable, so every setup logs an expected probe warning.
logging.getLogger(PACKAGE).setLevel(logging.ERROR)


class FakeSession:
    """MA connections that stay open until cancelled, and an MA HTTP API that is down."""

    def __init__(self) -> None:
        self.open_connections = 0
        self.connects = 0

    async def ws_connect(self, url, **kwargs):
        self.connects += 1
        self.open_connections += 1
        try:
            await asyncio.Event().wait()
        finally:
            self.open_connections -= 1

    def get(self, url, **kwargs):
        class Failing:
            async def __aenter__(self):
                raise ClientError("down")

            async def __aexit__(self, *exc):
                return False

        return Failing()


class Services:
    def __init__(self) -> None:
        self.registered: dict[tuple[str, str], Any] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def has_service(self, domain, service):
        return (domain, service) in self.registered or domain in {"media_player", "light"}

    def async_register(self, domain, service, handler, schema=None):
        self.registered[(domain, service)] = handler

    async def async_call(self, domain, service, data=None, blocking=False, target=None, return_response=False):
        self.calls.append((domain, service, dict(data or {})))


class Bus:
    def __init__(self) -> None:
        self.fired: list[str] = []

    def async_listen_once(self, event_type, action):
        return HANDLES.add(f"bus:{event_type}", action, once=True)

    def async_fire(self, event_type, data=None):
        self.fired.append(event_type)


class Http:
    def __init__(self) -> None:
        self.views: list[Any] = []
        self.static_paths: list[Any] = []

    def register_view(self, view) -> None:
        self.views.append(view)

    async def async_register_static_paths(self, paths) -> None:
        self.static_paths.extend(paths)


class ConfigEntries:
    def __init__(self) -> None:
        self.forward_error: Exception | None = None

    def async_entries(self, domain=None):
        return []

    async def async_forward_entry_setups(self, entry, platforms):
        if self.forward_error is not None:
            raise self.forward_error

    async def async_unload_platforms(self, entry, platforms):
        return True

    async def async_reload(self, entry_id):
        raise AssertionError("not used")


class FakeHass:
    def __init__(self, stored: dict[str, Any] | None = None) -> None:
        self.data: dict[str, Any] = {}
        self.stored: dict[str, Any] = copy.deepcopy(stored or {})
        self.saves: dict[str, int] = {}
        self.session = FakeSession()
        self.services = Services()
        self.bus = Bus()
        self.http = Http()
        self.config_entries = ConfigEntries()
        self.states = SimpleNamespace(get=self._state)
        self.player_volume = 0.9
        self.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=None))
        self.background_tasks: list[asyncio.Task] = []

    def _state(self, entity_id):
        if entity_id == KITCHEN:
            return SimpleNamespace(state="idle", attributes={"volume_level": self.player_volume})
        return None

    def async_create_task(self, target, name=None, eager_start=True):
        return asyncio.get_running_loop().create_task(target, name=name)

    def async_create_background_task(self, target, name, eager_start=True):
        task = asyncio.get_running_loop().create_task(target, name=name)
        self.background_tasks.append(task)
        return task

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class Entry:
    def __init__(self, entry_id: str = "entry-1") -> None:
        self.entry_id = entry_id
        self.title = "HOMEii Flow Engine"
        self.data = {"instance_id": "default"}
        self.options = {"music_assistant_url": MA_URL, "music_assistant_token": "ma-token"}
        self.unload_callbacks: list[Any] = []

    def add_update_listener(self, listener):
        return lambda: None

    def async_on_unload(self, callback) -> None:
        self.unload_callbacks.append(callback)


def stored_engine_data() -> dict[str, Any]:
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    return {
        "maverick_music_flow.storage": {
            "volume_rules": [{"profile_id": "default", "player": KITCHEN, "max_volume": 30, "enabled": True}],
            "timers": [{"profile_id": "default", "id": "sleep", "player": KITCHEN, "action": "stop", "ends_at": past}],
            "schedules": [{"profile_id": "default", "id": "wake", "player": KITCHEN, "time": "07:00", "enabled": True}],
            "artwork_lighting": {
                KITCHEN: {"lights": ["light.desk"], "enabled": True, "brightness": 35, "transition": 3, "cooldown": 8}
            },
        }
    }


class LifecycleTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        HANDLES.all.clear()
        HANDLES.double_removals = 0
        WS_COMMANDS.clear()
        self.hass = FakeHass(stored_engine_data())
        self.patches = {
            # Unrelated to lifecycle and would need the whole player snapshot.
            "async_update_playback_statistics": AsyncMock(return_value={}),
            "async_execute_schedule": AsyncMock(return_value={"ok": True}),
            "_player_readiness": lambda self, player: {"ready": True},
        }
        cls = RUNTIME_MODULE.HomeiiFlowRuntime
        self.originals = {name: cls.__dict__[name] for name in self.patches}
        for name, value in self.patches.items():
            setattr(cls, name, value)
        self.addAsyncCleanup(self._cleanup)

    async def _cleanup(self) -> None:
        runtime = self.hass.data.get(DOMAIN, {}).get("runtime")
        if runtime is not None:
            runtime._entries.clear()
            await runtime.async_shutdown()
        for name, value in self.originals.items():
            setattr(RUNTIME_MODULE.HomeiiFlowRuntime, name, value)

    async def settle(self) -> None:
        for _ in range(20):
            await asyncio.sleep(0)

    async def setup_entry(self, entry: Entry | None = None) -> Entry:
        entry = entry or Entry()
        self.assertTrue(await ENGINE.async_setup_entry(self.hass, entry))
        await self.settle()
        return entry

    async def unload_entry(self, entry: Entry) -> None:
        self.assertTrue(await ENGINE.async_unload_entry(self.hass, entry))
        await self.settle()

    def runtime(self):
        return self.hass.data[DOMAIN]["runtime"]

    def service_calls(self, service: str) -> list[tuple[str, str, dict[str, Any]]]:
        return [call for call in self.hass.services.calls if call[1] == service]

    def ma_task(self):
        return self.runtime()._music_assistant_client._task


class SetupTests(LifecycleTestCase):
    def test_config_entry_only_schema(self) -> None:
        self.assertEqual(ENGINE.CONFIG_SCHEMA, "config-entry-only")
        self.assertIn(DOMAIN, SCHEMA_CALLS)

    async def test_async_setup_registers_only_services(self) -> None:
        self.assertTrue(await ENGINE.async_setup(self.hass, {}))
        self.assertNotIn("runtime", self.hass.data.get(DOMAIN, {}))
        self.assertEqual(self.hass.http.views, [])
        self.assertEqual(self.hass.http.static_paths, [])
        self.assertEqual(WS_COMMANDS, [])
        self.assertEqual(HANDLES.all, [])
        self.assertEqual(self.hass.saves, {})
        self.assertIn((DOMAIN, "set_volume_rule"), self.hass.services.registered)
        self.assertIn((DOMAIN, "set_queue_settings"), self.hass.services.registered)
        call = SimpleNamespace(context=SimpleNamespace(user_id=None), data={"player": KITCHEN, "max_volume": 20})
        for service in ("set_volume_rule", "set_queue_settings"):
            with self.subTest(service=service):
                with self.assertRaises(ServiceValidationError) as caught:
                    await self.hass.services.registered[(DOMAIN, service)](call)
                self.assertEqual(caught.exception.kwargs["translation_key"], "not_loaded")
        self.assertNotIn("runtime", self.hass.data.get(DOMAIN, {}))

    async def test_setup_entry_starts_the_runtime_and_registers_endpoints(self) -> None:
        await ENGINE.async_setup(self.hass, {})
        await self.setup_entry()
        runtime = self.runtime()
        self.assertTrue(runtime.active)
        self.assertEqual(len(self.hass.http.views), 4)
        self.assertEqual(len(self.hass.http.static_paths), 1)
        self.assertTrue(WS_COMMANDS)
        counts = HANDLES.counts()
        self.assertEqual(counts["interval:30"], 1)  # orchestration tick
        self.assertEqual(counts["time_change"], 1)  # minute tick
        self.assertEqual(counts["interval:10"], 1)  # artwork lighting reconcile
        self.assertEqual(counts["state_change"], 1)  # artwork lighting player listener
        self.assertEqual(counts["bus:homeassistant_stop"], 1)
        self.assertFalse(self.ma_task().done())
        self.assertEqual(self.hass.session.open_connections, 1)
        # The startup tick ran the overdue timer and the volume rule while loaded.
        self.assertEqual(len(self.service_calls("media_stop")), 1)
        self.assertEqual(len(self.service_calls("volume_set")), 1)


class UnloadTests(LifecycleTestCase):
    async def test_unload_stops_every_timer_listener_task_and_connection(self) -> None:
        entry = await self.setup_entry()
        runtime = self.runtime()
        client = runtime._music_assistant_client
        ma_task = self.ma_task()
        self.hass.player_volume = 0.9
        HANDLES.fire("interval:30", datetime.now(UTC))
        await self.settle()
        self.assertEqual(len(self.service_calls("volume_set")), 2)  # control: a tick acts while loaded

        await self.unload_entry(entry)

        self.assertFalse(runtime.active)
        self.assertEqual(HANDLES.active(), [])
        self.assertEqual(HANDLES.double_removals, 0)
        self.assertEqual(runtime._background_tasks, set())
        self.assertTrue(ma_task.done())
        self.assertIsNone(client._task)
        self.assertEqual(self.hass.session.open_connections, 0)
        self.assertFalse(client.snapshot()["token_configured"])
        lighting = runtime.artwork_lighting
        self.assertIsNone(lighting._unsub)
        self.assertIsNone(lighting._interval)
        self.assertIsNone(lighting._stop_unsub)
        self.assertEqual(lighting._tasks, {})
        self.assertEqual(runtime._schedule_manager.state, "stopped")
        self.assertEqual(runtime._schedule_unsubs, {})

    async def test_nothing_acts_on_schedules_timers_or_volume_rules_after_unload(self) -> None:
        entry = await self.setup_entry()
        runtime = self.runtime()
        HANDLES.fire("call_later", datetime.now(UTC))  # scheduler ready, delayed startup ticks
        await self.settle()
        await self.unload_entry(entry)
        calls_before = list(self.hass.services.calls)
        schedule_runs = runtime.async_execute_schedule.await_count
        # A due timer, a due schedule and a player above its volume limit.
        runtime._storage["timers"].append(
            {"profile_id": "default", "id": "late", "player": KITCHEN, "ends_at": datetime.now(UTC).isoformat()}
        )
        now = datetime.now(UTC)
        runtime._storage["schedules"].append(
            {"profile_id": "default", "id": "now", "player": KITCHEN, "time": now.strftime("%H:%M"), "enabled": True}
        )
        self.hass.player_volume = 1.0
        for kind in ("interval:30", "time_change", "call_later", "point_in_time", "interval:10", "state_change"):
            self.assertEqual(HANDLES.fire(kind, now), 0, kind)
        result = await runtime.async_tick_orchestration(now, trigger="manual")
        await self.settle()
        self.assertEqual((result["schedules"], result["timers"], result["volume_rules"]), ([], [], []))
        self.assertEqual(self.hass.services.calls, calls_before)
        self.assertEqual(runtime.async_execute_schedule.await_count, schedule_runs)
        with self.assertRaises(RUNTIME_MODULE.HomeiiFlowServiceUnavailable):
            runtime.async_create_tracked_task(asyncio.sleep(0), "late")

    async def test_in_flight_background_tasks_are_cancelled_and_forgotten(self) -> None:
        entry = await self.setup_entry()
        runtime = self.runtime()
        started = asyncio.Event()

        async def hang(**_kwargs):
            started.set()
            await asyncio.Event().wait()

        runtime._player_snapshot_for_entity = lambda entity_id: None
        runtime._try_music_queue_command_bridge = hang
        caller = asyncio.create_task(runtime.async_get_queue({"entity_id": KITCHEN, "queue_id": "q"}))
        await started.wait()
        (inflight,) = runtime._queue_inflight.values()
        self.assertIn(inflight, runtime._background_tasks)
        # The card gave up first, so nobody removes the in-flight entry (finding B-4).
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertFalse(inflight.done())

        await self.unload_entry(entry)

        self.assertTrue(inflight.cancelled())
        self.assertEqual(runtime._queue_inflight, {})

        # After a reload the next request starts a fresh fetch instead of the cancelled one.
        await self.setup_entry(entry)

        async def answer(**_kwargs):
            return [{"normalized": {"items": [], "queue_id": "q"}, "provider": "test"}]

        runtime._try_music_queue_command_bridge = answer
        runtime._queue_result_with_snapshot = lambda result, **_kwargs: result
        result = await runtime.async_get_queue({"entity_id": KITCHEN, "queue_id": "q"})
        self.assertEqual(result["provider"], "test")

    async def test_long_lived_tasks_are_home_assistant_background_tasks(self) -> None:
        entry = await self.setup_entry()
        runtime = self.runtime()
        relay = runtime.async_create_tracked_task(asyncio.Event().wait(), "relay", background=True)
        self.assertEqual(self.hass.background_tasks, [relay])
        await self.unload_entry(entry)
        self.assertTrue(relay.cancelled())

    async def test_unload_flushes_pending_saves(self) -> None:
        entry = await self.setup_entry()
        runtime = self.runtime()
        runtime._library_cache[("playlist", "", 60, False, "", 0)] = {
            "fresh_until": 0.0, "stale_until": 0.0, "stored_at": 1.0, "result": {"items": []},
        }
        runtime._schedule_media_cache_save()  # debounced two seconds
        runtime._storage["activity"] = [{"kind": "unsaved"}]  # changed by a task that was cancelled
        media_saves = self.hass.saves.get("maverick_music_flow.media_cache", 0)

        await self.unload_entry(entry)

        self.assertEqual(HANDLES.active("call_later"), [])
        self.assertEqual(self.hass.saves["maverick_music_flow.media_cache"], media_saves + 1)
        self.assertEqual(len(self.hass.stored["maverick_music_flow.media_cache"]["entries"]), 1)
        self.assertEqual(self.hass.stored["maverick_music_flow.storage"]["activity"], [{"kind": "unsaved"}])

    async def test_views_and_websocket_commands_refuse_after_unload(self) -> None:
        entry = await self.setup_entry()
        await self.unload_entry(entry)
        views = {type(view).__name__: view for view in self.hass.http.views}
        request = {"hass_user": SimpleNamespace(id="user", is_admin=True)}
        with self.assertRaises(HTTPNotFound):
            await views["HomeiiFlowItemArtworkProxyView"].get(request, "token")
        with self.assertRaises(HTTPServiceUnavailable):
            await views["HomeiiFlowArtworkProxyView"].get(request, KITCHEN)
        with self.assertRaises(HTTPServiceUnavailable):
            await views["HomeiiFlowCommandView"].post(request, "get_context")
        with self.assertRaises(HTTPServiceUnavailable):
            await views["HomeiiFlowSendspinView"].get(request, "ma_homeii_device")
        self.assertEqual(self.hass.session.connects, 1)  # only the MA event stream, before unload
        connection = SimpleNamespace(
            user=SimpleNamespace(is_admin=True), send_result=AsyncMock(), errors=[],
            send_error=lambda msg_id, code, message: connection.errors.append(code),
        )
        handler = next(h for h in WS_COMMANDS if h.__name__ == "websocket_get_context")
        handler(self.hass, connection, {"id": 1, "type": "maverick_music_flow/get_context"})
        self.assertEqual(connection.errors, ["not_loaded"])
        connection.send_result.assert_not_called()

    async def test_unloading_one_of_two_entries_keeps_the_engine_running(self) -> None:
        first = await self.setup_entry(Entry("entry-1"))
        counts = HANDLES.counts()
        second = await self.setup_entry(Entry("entry-2"))
        self.assertEqual(HANDLES.counts(), counts)
        await self.unload_entry(first)
        self.assertTrue(self.runtime().active)
        self.assertEqual(HANDLES.counts(), counts)
        self.assertFalse(self.ma_task().done())
        await self.unload_entry(second)
        self.assertFalse(self.runtime().active)
        self.assertEqual(HANDLES.active(), [])

    async def test_failed_setup_stops_the_runtime_again(self) -> None:
        self.hass.config_entries.forward_error = RuntimeError("platform failed")
        with self.assertRaises(RuntimeError):
            await ENGINE.async_setup_entry(self.hass, Entry())
        await self.settle()
        runtime = self.runtime()
        self.assertFalse(runtime.active)
        self.assertEqual(runtime.entries, [])
        self.assertEqual(HANDLES.active(), [])
        self.assertEqual(self.hass.session.open_connections, 0)


class ReloadTests(LifecycleTestCase):
    async def test_reload_creates_exactly_one_set_of_timers_and_listeners(self) -> None:
        entry = await self.setup_entry()
        first_counts = HANDLES.counts()
        views, commands = len(self.hass.http.views), len(WS_COMMANDS)
        for _ in range(3):
            await self.unload_entry(entry)
            self.assertEqual(HANDLES.active(), [])
            await self.setup_entry(entry)
            self.assertTrue(self.runtime().active)
            self.assertEqual(HANDLES.counts(), first_counts)
        self.assertEqual(HANDLES.double_removals, 0)
        self.assertEqual(len(self.hass.http.views), views)
        self.assertEqual(len(self.hass.http.static_paths), 1)
        self.assertEqual(len(WS_COMMANDS), commands)
        self.assertEqual(self.hass.session.open_connections, 1)
        self.assertFalse(self.ma_task().done())
        # One interval tick after the reloads runs orchestration exactly once.
        runtime = self.runtime()
        original = runtime.async_tick_orchestration
        ticks: list[str] = []

        async def counting_tick(now=None, *, trigger="manual"):
            ticks.append(trigger)
            return await original(now, trigger=trigger)

        runtime.async_tick_orchestration = counting_tick
        HANDLES.fire("interval:30", datetime.now(UTC))
        await self.settle()
        self.assertEqual(ticks, ["interval"])

    async def test_home_assistant_stop_after_reload_does_not_remove_a_listener_twice(self) -> None:
        entry = await self.setup_entry()
        await self.unload_entry(entry)
        await self.setup_entry(entry)
        self.assertEqual(HANDLES.fire("bus:homeassistant_stop", None), 1)
        self.assertIsNone(self.runtime().artwork_lighting._interval)
        await self.unload_entry(entry)
        self.assertEqual(HANDLES.double_removals, 0)
        self.assertEqual(HANDLES.active(), [])


if __name__ == "__main__":
    main()
