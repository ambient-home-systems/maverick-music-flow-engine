"""Exercise the Music Assistant command bridge: allowlist, internal flags and the HTTP view.

The real policy module, runtime method, WebSocket helper and HTTP view run against fakes
at the Music Assistant and aiohttp boundaries, without installing Home Assistant. Requests
here come from an administrator; test_authorization.py covers other users.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import importlib
import re
import runpy
import sys
import time
import types
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import IsolatedAsyncioTestCase, TestCase, main
from unittest.mock import AsyncMock, Mock

import voluptuous as vol

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/maverick_music_flow"
FUTURE = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)


def load_command_bridge():
    """Import command_bridge.py and const.py without running the HA package __init__."""
    package = "_mmf_command_bridge_under_test"
    if package not in sys.modules:
        stub = types.ModuleType(package)
        stub.__path__ = [str(COMPONENT)]
        sys.modules[package] = stub
    return importlib.import_module(f"{package}.command_bridge")


BRIDGE = load_command_bridge()
AUTHORIZATION = importlib.import_module(f"{BRIDGE.__name__.rsplit('.', 1)[0]}.authorization")
ALLOWLIST = BRIDGE.MUSIC_ASSISTANT_COMMAND_ALLOWLIST
allowed = BRIDGE.music_assistant_command_allowed

# Commands the HOMEii Music Flow card sends through ma/command
# (r11a/homeii-music-flow, src/core, checked at commit 7380d67).
CARD_COMMANDS = {
    "ai_radio/hosts/list",
    "ai_radio/queue_dj/set",
    "ai_radio/queue_dj/status",
    "audio_analysis/wave_form",
    "metadata/get_track_lyrics",
    "music/albums/album_tracks",
    "music/albums/library_items",
    "music/artists/artist_albums",
    "music/artists/artist_tracks",
    "music/browse",
    "music/favorites/add_item",
    "music/favorites/remove_item",
    "music/genres/library_items",
    "music/in_progress_items",
    "music/item_by_uri",
    "music/library/add_item",
    "music/playlists/add_playlist_tracks",
    "music/playlists/library_items",
    "music/playlists/playlist_tracks",
    "music/podcasts/podcast_episodes",
    "music/radios/library_items",
    "music/recently_played_items",
    "music/recommendations",
    "music/recommendations/items",
    "music/search",
    "music/tracks/library_items",
    "music/tracks/similar_tracks",
    "player_queues/crossfade",
    "player_queues/delete_item",
    "player_queues/get",
    "player_queues/items",
    "player_queues/move_item",
    "player_queues/play_index",
    "player_queues/play_media",
    "player_queues/shuffle",
    "players/all",
}

DENIED_COMMANDS = [
    # Configuration and authentication
    "config/core/get",
    "config/core/save",
    "config/players/save",
    "config/providers/save",
    "config/providers/remove",
    "auth/login",
    "auth/users",
    "auth/token/create",
    # Providers
    "providers/add",
    "providers/remove",
    "providers/manifests",
    # Player and group administration
    "players/remove",
    "players/create_group_player",
    "players/remove_group_player",
    "players/add_currently_playing_to_favorites",
    # Sync, import and export
    "music/start_sync",
    "music/sync",
    "music/import",
    "music/export",
    "music/playlists/import",
    # Library and playlist administration outside what the card uses
    "music/add_item_to_library",
    "music/library/remove_item",
    "music/playlists/create_playlist",
    "music/playlists/remove_playlist_tracks",
    "music/tracks/update",
    "music/tracks/remove",
    "music/refresh_item",
    "music/delete",
    # Other namespaces and unused reads
    "info",
    "metadata/update_metadata",
    "audio_analysis/analyze",
    "ai_radio/hosts/create",
    # Unknown, malformed and look-alike names
    "",
    "players",
    "players/",
    "Players/all",
    " players/all",
    "players/all/",
    "players/all/../../config/core/save",
    "music/search?x=1",
    "unknown/command",
]


def bridge_command_literals(source: str) -> set[str]:
    """Return Music Assistant command names written as string literals in Engine source."""
    pattern = r'"((?:players|player_queues|music|ai_radio|metadata|audio_analysis)/[a-z_/]*[a-z_])"'
    return set(re.findall(pattern, source))


class AllowlistTests(TestCase):
    def test_every_allowlisted_command_is_accepted(self):
        self.assertIsInstance(ALLOWLIST, frozenset)
        for command in sorted(ALLOWLIST):
            with self.subTest(command=command):
                self.assertTrue(allowed(command))

    def test_admin_config_provider_sync_and_unknown_commands_are_denied(self):
        for command in DENIED_COMMANDS:
            with self.subTest(command=command):
                self.assertFalse(allowed(command))
                self.assertNotIn(command, ALLOWLIST)

    def test_no_administrative_namespace_is_allowlisted(self):
        for command in ALLOWLIST:
            with self.subTest(command=command):
                self.assertFalse(command.startswith(("config/", "auth/", "providers/")))
                for token in (
                    "sync",
                    "import",
                    "export",
                    "create",
                    "remove_group",
                    "/remove",
                    "update",
                ):
                    self.assertNotIn(token, command.replace("remove_item", ""))

    def test_commands_the_engine_sends_are_allowlisted(self):
        # queue_settings.py talks to the MA client directly for config/core/*, not the bridge.
        literals: set[str] = set()
        for name in ("runtime.py", "queue_controls.py", "saved_playlists.py", "websocket_api.py"):
            literals |= bridge_command_literals((COMPONENT / name).read_text(encoding="utf-8"))
        self.assertIn("players/cmd/play", literals)
        self.assertEqual(set(), literals - ALLOWLIST)

    def test_every_library_root_the_engine_uses_is_allowlisted(self):
        tree = ast.parse((COMPONENT / "runtime.py").read_text(encoding="utf-8"))
        cls = next(
            n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HomeiiFlowRuntime"
        )
        method = next(
            n
            for n in cls.body
            if isinstance(n, ast.FunctionDef) and n.name == "_media_type_command_roots"
        )
        mapping = next(
            n.value
            for n in ast.walk(method)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "roots_by_type" for t in n.targets)
        )
        roots = {root for roots in ast.literal_eval(mapping).values() for root in roots}
        self.assertTrue(roots)
        for root in roots:
            self.assertIn(f"music/{root}/library_items", ALLOWLIST)

    def test_commands_the_card_sends_are_allowlisted(self):
        self.assertEqual(set(), CARD_COMMANDS - ALLOWLIST)

    def test_denied_commands_are_logged_at_debug_by_name_only(self):
        with self.assertLogs(BRIDGE._LOGGER, level="DEBUG") as logs:
            self.assertFalse(allowed("music/start_sync"))
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].levelname, "DEBUG")
        self.assertIn("music/start_sync", logs.output[0])

    def test_allowed_commands_are_not_logged(self):
        with self.assertNoLogs(BRIDGE._LOGGER, level="DEBUG"):
            self.assertTrue(allowed("players/all"))


class InternalKeyTests(TestCase):
    def test_strip_internal_keys_removes_every_homeii_prefixed_key(self):
        payload = {
            "command": "music/item_by_uri",
            "args": {"uri": "library://track/1"},
            "_homeii_cache_worker": True,
            "_homeii_cache_refresh": True,
            "_homeii_anything": 1,
            "homeii_public": "kept",
        }
        clean = BRIDGE.strip_internal_keys(payload)
        self.assertEqual(
            clean,
            {
                "command": "music/item_by_uri",
                "args": {"uri": "library://track/1"},
                "homeii_public": "kept",
            },
        )
        self.assertIn("_homeii_cache_worker", payload)

    def test_websocket_command_payload_drops_id_type_and_internal_flags(self):
        source = COMPONENT / "websocket_api.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        nodes = [
            n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_command_payload"
        ]
        ns: dict[str, Any] = {"Any": Any, "strip_internal_keys": BRIDGE.strip_internal_keys}
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[FUTURE, *nodes], type_ignores=[])),
                str(source),
                "exec",
            ),
            ns,
        )
        msg = {
            "id": 7,
            "type": "maverick_music_flow/ma/command",
            "command": "players/all",
            "args": {},
            "_homeii_cache_worker": True,
        }
        self.assertEqual(ns["_command_payload"](msg), {"command": "players/all", "args": {}})


MEDIA_POLICY = runpy.run_path(str(COMPONENT / "media_url_policy.py"))


def load_runtime_command_method():
    source = COMPONENT / "runtime.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HomeiiFlowRuntime"
    )
    cls.decorator_list = []
    wanted = {
        "async_music_assistant_command",
        "_music_assistant_command_cacheable",
        "_music_assistant_command_cache_key",
        "_finish_media_command_refresh",
        "_async_validate_media_reference",
        "local_media_urls_allowed",
        "async_create_tracked_task",
    }
    cls.body = [
        n
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in wanted
    ]
    ns: dict[str, Any] = {
        "Any": Any,
        "asyncio": asyncio,
        "copy": copy,
        "hashlib": hashlib,
        "time": time,
        "music_assistant_command_allowed": allowed,
        "command_media_references": MEDIA_POLICY["command_media_references"],
        "async_validate_media_reference": MEDIA_POLICY["async_validate_media_reference"],
        "home_assistant_base_url": lambda _hass: "",
        "HomeiiFlowServiceUnavailable": RuntimeError,
        "_utc_iso": lambda: "now",
    }
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[FUTURE, cls], type_ignores=[])),
            str(source),
            "exec",
        ),
        ns,
    )
    return ns["HomeiiFlowRuntime"]


Runtime = load_runtime_command_method()


class RuntimeCommandTests(IsolatedAsyncioTestCase):
    def runtime(self):
        runtime = Runtime()
        runtime.hass = SimpleNamespace(
            async_create_task=lambda coro, name=None: asyncio.get_running_loop().create_task(coro, name=name)
        )
        runtime._active = True
        runtime._background_tasks = set()
        runtime._music_assistant_client = SimpleNamespace(
            snapshot=lambda: {"authenticated": True, "schema_supported": True, "connected": True},
            async_command=AsyncMock(return_value={"name": "fresh"}),
        )
        runtime._ma_http_health = {}
        runtime.music_assistant_base_urls = lambda: []
        runtime._matching_entry = lambda _instance_id=None: None
        runtime._media_cache_metrics = defaultdict(int)
        runtime._media_command_cache = {}
        runtime._media_command_inflight = {}
        runtime.decorate_artwork_urls = lambda value: value
        runtime._schedule_media_cache_save = Mock()
        runtime._mark_library_cache_stale = Mock()
        runtime._mark_media_command_cache_stale = Mock()
        return runtime

    def seed_cache(self, runtime, command, args, result):
        key = runtime._music_assistant_command_cache_key(command, args)
        now = time.monotonic()
        runtime._media_command_cache[key] = {
            "fresh_until": now + 600,
            "stale_until": now + 3600,
            "stored_at": time.time(),
            "result": result,
            "persistent": False,
            "invalidated": False,
        }

    async def test_denied_command_never_reaches_music_assistant(self):
        runtime = self.runtime()
        for command in ("music/start_sync", "config/core/save", "players/remove", "info"):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, "not allowed"):
                await runtime.async_music_assistant_command({"command": command, "args": {}})
        runtime._music_assistant_client.async_command.assert_not_awaited()

    async def test_payload_cache_flags_cannot_bypass_the_cache(self):
        runtime = self.runtime()
        args = {"uri": "library://track/1"}
        self.seed_cache(runtime, "music/item_by_uri", args, {"name": "cached"})
        result = await runtime.async_music_assistant_command(
            {
                "command": "music/item_by_uri",
                "args": args,
                "_homeii_cache_worker": True,
                "_homeii_cache_refresh": True,
            }
        )
        self.assertEqual(result, {"name": "cached"})
        runtime._music_assistant_client.async_command.assert_not_awaited()
        self.assertEqual(runtime._media_cache_metrics["command_hits"], 1)

    async def test_payload_cache_flags_cannot_bypass_request_coalescing(self):
        runtime = self.runtime()
        release = asyncio.Event()

        async def slow_command(*_args, **_kwargs):
            await release.wait()
            return {"name": "fresh"}

        runtime._music_assistant_client.async_command = AsyncMock(side_effect=slow_command)
        payload = {
            "command": "music/item_by_uri",
            "args": {"uri": "library://track/2"},
            "_homeii_cache_worker": True,
        }
        first = asyncio.create_task(runtime.async_music_assistant_command(dict(payload)))
        second = asyncio.create_task(runtime.async_music_assistant_command(dict(payload)))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second)
        self.assertEqual(runtime._music_assistant_client.async_command.await_count, 1)
        self.assertEqual([r["data"] for r in results], [{"name": "fresh"}, {"name": "fresh"}])
        self.assertEqual(runtime._media_cache_metrics["coalesced"], 1)
        self.assertEqual(len(runtime._media_command_cache), 1)

    async def test_internal_keyword_flags_still_drive_the_cache_worker(self):
        runtime = self.runtime()
        args = {"uri": "library://track/3"}
        self.seed_cache(runtime, "music/item_by_uri", args, {"name": "cached"})
        result = await runtime.async_music_assistant_command(
            {"command": "music/item_by_uri", "args": args}, cache_worker=True, cache_refresh=True
        )
        self.assertEqual(result["data"], {"name": "fresh"})
        runtime._music_assistant_client.async_command.assert_awaited_once()

    async def test_stale_entry_refreshes_in_background_with_internal_flags(self):
        runtime = self.runtime()
        args = {"uri": "library://track/4"}
        self.seed_cache(runtime, "music/item_by_uri", args, {"name": "stale"})
        key = runtime._music_assistant_command_cache_key("music/item_by_uri", args)
        runtime._media_command_cache[key]["fresh_until"] = 0
        result = await runtime.async_music_assistant_command(
            {"command": "music/item_by_uri", "args": args}
        )
        self.assertEqual(result, {"name": "stale"})
        await runtime._media_command_inflight[key]
        runtime._music_assistant_client.async_command.assert_awaited_once()
        self.assertEqual(runtime._media_command_cache[key]["result"]["data"], {"name": "fresh"})


class HTTPError(Exception):
    def __init__(self, text=""):
        super().__init__(text)
        self.text = text


class NotFound(HTTPError):
    pass


class BadRequest(HTTPError):
    pass


class Forbidden(HTTPError):
    pass


class ServiceUnavailable(HTTPError):
    pass


class AdminUser:
    """An administrator with Home Assistant's default policy (every entity allowed)."""

    is_admin = True
    permissions = SimpleNamespace(check_entity=lambda entity_id, key: True)


class FakeRequest(dict):
    def __init__(self, body):
        super().__init__(hass_user=AdminUser())
        self._body = body

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeRuntime:
    def __init__(self):
        self.calls: list[tuple[str, Any]] = []
        self.active = True

    def _record(self, name, payload):
        self.calls.append((name, payload))
        return {"method": name}

    def context(self, **kwargs):
        return self._record("context", kwargs)

    def bootstrap_snapshot(self, **kwargs):
        return self._record("bootstrap_snapshot", kwargs)

    async def async_get_queue(self, payload):
        return self._record("async_get_queue", payload)

    async def async_get_library(self, payload):
        return self._record("async_get_library", payload)

    async def async_get_favorites(self, payload):
        return self._record("async_get_favorites", payload)

    async def async_set_favorite(self, payload):
        return self._record("async_set_favorite", payload)

    async def async_get_search(self, payload):
        return self._record("async_get_search", payload)

    async def async_music_assistant_command(self, payload, **kwargs):
        self.calls.append(("async_music_assistant_command", payload))
        self.calls.append(("kwargs", kwargs))
        return {"method": "async_music_assistant_command"}

    def control_entity_id(self, target):
        return target if str(target).startswith("media_player.") else f"media_player.{target}"


def load_command_view(runtime):
    source = COMPONENT / "__init__.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    nodes = [
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HomeiiFlowCommandView"
    ]
    ns: dict[str, Any] = {
        "vol": vol,
        "HomeAssistantView": object,
        "HomeAssistant": object,
        "web": SimpleNamespace(
            Request=object,
            Response=object,
            HTTPNotFound=NotFound,
            HTTPBadRequest=BadRequest,
            HTTPForbidden=Forbidden,
            HTTPServiceUnavailable=ServiceUnavailable,
            json_response=lambda result: {"json": result},
        ),
        "NOT_LOADED_MESSAGE": "HOMEii Flow Engine is not loaded",
        "HTTP_COMMAND_SCHEMAS": BRIDGE.HTTP_COMMAND_SCHEMAS,
        "music_assistant_command_allowed": allowed,
        "strip_internal_keys": BRIDGE.strip_internal_keys,
        "async_get_runtime": lambda _hass: runtime,
        "CONF_INSTANCE_ID": "instance_id",
        "CONF_PROFILE_ID": "profile_id",
        "POLICY_CONTROL": "control",
        "access_denial": AUTHORIZATION.access_denial,
        "http_access_level": AUTHORIZATION.http_access_level,
        "payload_targets": AUTHORIZATION.payload_targets,
        "requires_target": AUTHORIZATION.requires_target,
    }
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[FUTURE, *nodes], type_ignores=[])),
            str(source),
            "exec",
        ),
        ns,
    )
    return ns["HomeiiFlowCommandView"](hass=None)


class CommandViewTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = FakeRuntime()
        self.view = load_command_view(self.runtime)

    async def post(self, command, body):
        return await self.view.post(FakeRequest(body), command)

    async def test_view_returns_503_while_no_entry_is_loaded(self):
        self.runtime.active = False
        for command, body in (("get_context", {}), ("ma/command", {"command": "players/all"}), ("unknown", {})):
            with self.subTest(command=command), self.assertRaises(ServiceUnavailable):
                await self.post(command, body)
        self.assertEqual(self.runtime.calls, [])

    async def test_http_view_serves_exactly_the_documented_commands(self):
        self.assertEqual(
            set(BRIDGE.HTTP_COMMAND_SCHEMAS),
            {
                "get_context",
                "bootstrap/get",
                "queue/get",
                "library/get",
                "favorites/get",
                "favorites/set",
                "search/get",
                "ma/command",
            },
        )

    async def test_unknown_commands_are_rejected(self):
        for command in (
            "schedules/set",
            "volume_rules/clear",
            "config/core/save",
            "ma/command/extra",
            "",
            "../get_context",
        ):
            with self.subTest(command=command), self.assertRaises(NotFound):
                await self.post(command, {})
        self.assertEqual(self.runtime.calls, [])

    async def test_unknown_keys_are_rejected(self):
        cases = [
            ("get_context", {"entity_id": "media_player.x"}),
            ("queue/get", {"queue_id": "q", "timeout": 60}),
            ("library/get", {"media_type": "album", "extra": 1}),
            ("favorites/set", {"favorite": True, "uri": "x", "surprise": 1}),
            ("search/get", {"query": "x", "providers": ["all"]}),
            ("ma/command", {"command": "players/all", "args": {}, "timeout": 60}),
        ]
        for command, body in cases:
            with self.subTest(command=command), self.assertRaises(BadRequest):
                await self.post(command, body)
        self.assertEqual(self.runtime.calls, [])

    async def test_invalid_values_and_missing_required_fields_are_rejected(self):
        cases = [
            ("library/get", {"limit": "many"}),
            ("library/get", {"offset": -1}),
            ("favorites/set", {"uri": "x"}),
            ("ma/command", {"args": {}}),
            ("ma/command", {"command": "players/all", "args": ["not", "a", "dict"]}),
        ]
        for command, body in cases:
            with self.subTest(command=command, body=body), self.assertRaises(BadRequest):
                await self.post(command, body)
        self.assertEqual(self.runtime.calls, [])

    async def test_card_message_is_accepted_and_type_is_dropped(self):
        body = {
            "type": "maverick_music_flow/library/get",
            "card_id": "living_room",
            "instance_id": "main",
            "profile_id": "default",
            "media_type": "album",
            "limit": 25,
        }
        result = await self.post("library/get", body)
        self.assertEqual(result, {"json": {"method": "async_get_library"}})
        name, payload = self.runtime.calls[0]
        self.assertEqual(name, "async_get_library")
        self.assertNotIn("type", payload)
        self.assertEqual(payload["media_type"], "album")
        self.assertEqual(payload["offset"], 0)

    async def test_get_context_passes_instance_and_profile(self):
        await self.post(
            "/get_context/", {"type": "maverick_music_flow/get_context", "instance_id": "main"}
        )
        self.assertEqual(
            self.runtime.calls, [("context", {"instance_id": "main", "profile_id": None})]
        )

    async def test_internal_flags_from_http_bodies_are_ignored(self):
        body = {
            "command": "music/item_by_uri",
            "args": {"uri": "library://track/1"},
            "_homeii_cache_worker": True,
            "_homeii_cache_refresh": True,
        }
        await self.post("ma/command", body)
        self.assertEqual(
            self.runtime.calls,
            [
                (
                    "async_music_assistant_command",
                    {"command": "music/item_by_uri", "args": {"uri": "library://track/1"}},
                ),
                ("kwargs", {}),
            ],
        )
        await self.post("queue/get", {"queue_id": "q", "_homeii_cache_refresh": True})
        self.assertEqual(self.runtime.calls[-1], ("async_get_queue", {"queue_id": "q"}))

    async def test_denied_ma_commands_are_forbidden(self):
        for command in (
            "music/start_sync",
            "config/core/save",
            "players/remove",
            "providers/add",
            "auth/login",
        ):
            with self.subTest(command=command), self.assertRaises(Forbidden):
                await self.post("ma/command", {"command": command, "args": {}})
        self.assertEqual(self.runtime.calls, [])

    async def test_allowed_ma_command_reaches_runtime_with_default_args(self):
        await self.post("ma/command", {"command": "players/all"})
        self.assertEqual(
            self.runtime.calls[0],
            ("async_music_assistant_command", {"command": "players/all", "args": {}}),
        )

    async def test_malformed_body_is_treated_as_empty(self):
        await self.post("get_context", ValueError("bad json"))
        self.assertEqual(self.runtime.calls[0][0], "context")
        with self.assertRaises(BadRequest):
            await self.post("favorites/set", ["not", "an", "object"])


if __name__ == "__main__":
    main()
