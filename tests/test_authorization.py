"""Exercise Engine authorization: the policy tables, every WebSocket handler, the HTTP
command view, the service guard and the runtime's player-to-entity mapping.

Home Assistant is not installed for these tests. websocket_api.py is imported with small
stand-ins for the Home Assistant modules it uses, so the real handlers run against a fake
connection and a fake runtime.
"""

from __future__ import annotations

import ast
import importlib
import re
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import IsolatedAsyncioTestCase, TestCase, main
from unittest.mock import AsyncMock, Mock

import voluptuous as vol

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/maverick_music_flow"
FUTURE = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
PACKAGE = "_mmf_authorization_under_test"
POLICY_CONTROL = "control"
ERR_UNAUTHORIZED = "unauthorized"


def _module(name: str, **attrs: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def _websocket_command(schema: dict[Any, Any]):
    """Stand-in for websocket_api.websocket_command that keeps the schema for tests.

    The command name is read from the source (see handler_commands) because two schemas
    reuse the "type" and "id" keys for other fields; see the PR notes.
    """

    def decorator(func):
        func._ws_schema = vol.Schema(schema)
        return func

    return decorator


_HA_STUBS = {
    "homeassistant": _module("homeassistant"),
    "homeassistant.auth": _module("homeassistant.auth"),
    "homeassistant.auth.permissions": _module("homeassistant.auth.permissions"),
    "homeassistant.auth.permissions.const": _module(
        "homeassistant.auth.permissions.const", POLICY_CONTROL=POLICY_CONTROL
    ),
    "homeassistant.components": _module("homeassistant.components"),
    "homeassistant.components.websocket_api": _module(
        "homeassistant.components.websocket_api",
        websocket_command=_websocket_command,
        async_response=lambda func: func,
        ActiveConnection=object,
        ERR_UNAUTHORIZED=ERR_UNAUTHORIZED,
    ),
    "homeassistant.core": _module(
        "homeassistant.core", HomeAssistant=object, callback=lambda func: func
    ),
}


def load_package_modules():
    """Import authorization.py and websocket_api.py without Home Assistant."""
    if PACKAGE in sys.modules:
        return sys.modules[f"{PACKAGE}.authorization"], sys.modules[f"{PACKAGE}.websocket_api"]
    stub = types.ModuleType(PACKAGE)
    stub.__path__ = [str(COMPONENT)]
    sys.modules[PACKAGE] = stub
    sys.modules[f"{PACKAGE}.runtime"] = _module(f"{PACKAGE}.runtime", HomeiiFlowRuntime=object)
    sys.modules[f"{PACKAGE}.radio_directory"] = _module(
        f"{PACKAGE}.radio_directory",
        search_stations=AsyncMock(return_value={"items": []}),
    )
    saved = {name: sys.modules.get(name) for name in _HA_STUBS}
    installed = {name for name, module in saved.items() if module is None}
    sys.modules.update({name: module for name, module in _HA_STUBS.items() if name in installed})
    try:
        authorization = importlib.import_module(f"{PACKAGE}.authorization")
        websocket_api = importlib.import_module(f"{PACKAGE}.websocket_api")
    finally:
        for name in installed:
            sys.modules.pop(name, None)
    return authorization, websocket_api


def handler_commands() -> dict[str, str]:
    """Return {handler function name: command type} from the websocket_command decorators."""
    tree = ast.parse((COMPONENT / "websocket_api.py").read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not (isinstance(decorator, ast.Call) and ast.unparse(decorator.func).endswith("websocket_command")):
                continue
            schema = decorator.args[0]
            for key, value in zip(schema.keys, schema.values):
                if (
                    isinstance(key, ast.Call)
                    and ast.unparse(key.func).endswith("Required")
                    and isinstance(key.args[0], ast.Constant)
                    and key.args[0].value == "type"
                ):
                    found[node.name] = value.value
    return found


AUTH, WS = load_package_modules()
HANDLERS = {command: getattr(WS, name) for name, command in handler_commands().items()}
PREFIX = AUTH.COMMAND_PREFIX
READ, CONTROL, MANAGE, ADMIN = (
    AUTH.ACCESS_READ,
    AUTH.ACCESS_CONTROL,
    AUTH.ACCESS_MANAGE,
    AUTH.ACCESS_ADMIN,
)

KITCHEN = "media_player.kitchen"
BEDROOM = "media_player.bedroom"
MA_PLAYERS = {"ma-kitchen": KITCHEN, "ma-bedroom": BEDROOM, "queue-bedroom": BEDROOM}


class FakeUser:
    def __init__(self, *, is_admin: bool = False, allowed: set[str] | None = None):
        self.id = "admin" if is_admin else "user"
        self.is_admin = is_admin
        # None mirrors Home Assistant's default policies, which allow every entity.
        self.allowed = allowed
        self.permissions = SimpleNamespace(check_entity=self.check_entity)
        self.checked: list[tuple[str, str]] = []

    def check_entity(self, entity_id: str, key: str) -> bool:
        self.checked.append((entity_id, key))
        return True if self.allowed is None else entity_id in self.allowed


class FakeConnection:
    def __init__(self, user: FakeUser):
        self.user = user
        self.send_result = Mock()
        self.send_error = Mock()

    @property
    def outcome(self) -> tuple[str, Any]:
        if self.send_error.called:
            return ("error", self.send_error.call_args.args[1])
        if self.send_result.called:
            return ("result", self.send_result.call_args.args[1])
        return ("none", None)


LIST_RESULTS = {
    "activity",
    "announcements",
    "schedule_summaries",
    "timer_summaries",
    "volume_rule_summaries",
    "active_volume_rule_summaries",
}


class FakeRuntime:
    """Records every Engine method a handler reaches after authorization."""

    def __init__(self, *, management_allowed: bool = False):
        self.management_allowed = management_allowed
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.stored = {"schedules": [], "timers": [], "volume_rules": []}
        self._storage: dict[str, Any] = {}
        self.async_save = AsyncMock()
        self.artwork_lighting = SimpleNamespace(
            snapshot=lambda: {"assignments": []},
            configure=AsyncMock(return_value={"ok": True}),
        )

    # Helpers _authorize and the playlist helpers rely on.
    def non_admin_management_allowed(self, instance_id=None) -> bool:
        return self.management_allowed

    def control_entity_id(self, target: str) -> str:
        clean = str(target or "").strip()
        if clean.startswith("media_player."):
            return clean
        return MA_PLAYERS.get(clean, f"media_player.homeii_{clean}")

    def _stored(self, kind: str, profile_id):
        return [
            item
            for item in self.stored[kind]
            if not profile_id or item.get("profile_id", "default") == profile_id
        ]

    def schedules(self, profile_id=None):
        return self._stored("schedules", profile_id)

    def timers(self, profile_id=None):
        return self._stored("timers", profile_id)

    def volume_rules(self, profile_id=None):
        return self._stored("volume_rules", profile_id)

    def _player_readiness(self, player):
        return {"ready": True}

    def _resolve_ma_player_id(self, player):
        return player

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        if name.startswith("async_"):

            async def recorder(*args, **kwargs):
                self.calls.append((name, args, kwargs))
                return {"queue_id": "q1", "entries": {}}

            return recorder

        def sync_recorder(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return [] if name in LIST_RESULTS else {}

        return sync_recorder


def registered_commands() -> set[str]:
    """Return every command type websocket_api.py registers, read from its source."""
    source = (COMPONENT / "websocket_api.py").read_text(encoding="utf-8")
    return set(re.findall(r'vol\.Required\("type"\): "(maverick_music_flow/[^"]+)"', source))


async def run(command: str, msg: dict[str, Any], user: FakeUser, runtime: FakeRuntime | None = None):
    """Validate msg like Home Assistant would, then run the registered handler."""
    runtime = runtime or FakeRuntime()
    handler = HANDLERS[PREFIX + command]
    full = {"id": 1, **handler._ws_schema({"type": PREFIX + command, **msg})}
    connection = FakeConnection(user)
    hass = SimpleNamespace(data={"maverick_music_flow": {"runtime": runtime}})
    result = handler(hass, connection, full)
    if hasattr(result, "__await__"):
        await result
    return connection, runtime


# Minimal valid messages for every command, by level.
READ_MESSAGES: dict[str, dict[str, Any]] = {
    "get_context": {},
    "bootstrap/get": {},
    "connections/get": {},
    "stats/get": {},
    "playback_stats/get": {},
    "players/get": {},
    "diagnostics/run": {},
    "orchestration/status": {},
    "queue/get": {},
    "library/get": {},
    "favorites/get": {},
    "search/get": {},
    "schedules/get": {},
    "timers/get": {},
    "volume_rules/get": {},
    "announcements/get": {},
    "activity/get": {},
    "screensaver/get": {},
    "sendspin/status": {},
    "lighting/get": {},
    "interface/get": {},
    "wheels/get": {},
    "radio/search": {},
    "queue/settings": {},
    "playlists": {},
}

CONTROL_MESSAGES: dict[str, dict[str, Any]] = {
    "playback/play_media": {"player": KITCHEN, "media_id": "library://track/1"},
    "player/command": {"player": KITCHEN, "command": "play"},
    "queue/action": {"entity_id": KITCHEN, "action": "remove", "queue_item_id": "item"},
    "queue/transfer": {"source_player": KITCHEN, "target_player": KITCHEN},
    "group/apply": {"owner": KITCHEN, "members": [KITCHEN]},
    "announce": {"message": "Dinner", "players": [KITCHEN]},
    "favorites/set": {"favorite": True, "uri": "library://track/1"},
    "screensaver/show": {},
    "ma/command": {"command": "players/cmd/play", "args": {"player_id": "ma-kitchen"}},
    "wheels/set": {"scope": "user", "context": "queue", "preference": {}},
    "playlists": {"action": "play", "playlist_id": "p1", "selected_player": KITCHEN},
}

MANAGE_MESSAGES: dict[str, dict[str, Any]] = {
    "schedules/set": {"player": KITCHEN, "time": "07:00"},
    "schedules/delete": {"schedule_id": "s1"},
    "schedules/run": {"schedule_id": "s1"},
    "timers/set": {"player": KITCHEN, "minutes": 10},
    "timers/delete": {"timer_id": "t1"},
    "volume_rules/set": {"player": KITCHEN, "max_volume": 40},
    "volume_rules/delete": {"player": KITCHEN},
    "volume_rules/clear": {},
}

ADMIN_MESSAGES: dict[str, dict[str, Any]] = {
    "orchestration/run_once": {},
    "screensaver/set": {"enabled": True},
    "lighting/set": {"player": KITCHEN, "lights": ["light.kitchen"]},
    "interface/set": {"night_mode": "on"},
    "queue/settings": {"values": {"autoplay_enabled": False}},
    "wheels/set": {"scope": "global", "context": "queue", "preference": {}},
    "playlists": {"action": "save", "name": "Dinner", "uris": ["library://track/1"]},
    "ma/command": {"command": "music/library/add_item", "args": {"item": "library://track/1"}},
}
ADMIN_MESSAGES_2: list[tuple[str, dict[str, Any]]] = [
    ("playlists", {"action": "delete", "playlist_id": "p1"}),
    ("ma/command", {"command": "music/playlists/add_playlist_tracks", "args": {"db_playlist_id": "1"}}),
]


class PolicyTableTests(TestCase):
    def test_every_registered_websocket_command_is_classified(self):
        registered = {command[len(PREFIX) :] for command in registered_commands()}
        self.assertEqual(registered, set(AUTH.WEBSOCKET_COMMAND_ACCESS))
        self.assertEqual(set(HANDLERS), registered_commands())
        self.assertTrue(all(hasattr(handler, "_ws_schema") for handler in HANDLERS.values()))
        self.assertGreaterEqual(len(registered), 45)
        for level in AUTH.WEBSOCKET_COMMAND_ACCESS.values():
            self.assertIn(level, AUTH.ACCESS_LEVELS)

    def test_every_handler_checks_authorization_before_anything_else(self):
        tree = ast.parse((COMPONENT / "websocket_api.py").read_text(encoding="utf-8"))
        handlers = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("websocket_")
        ]
        self.assertEqual(len(handlers), len(HANDLERS))
        for node in handlers:
            with self.subTest(handler=node.name):
                body = node.body
                if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                    body = body[1:]
                first = body[0]
                self.assertIsInstance(first, ast.If)
                self.assertEqual(ast.unparse(first.test), "not _authorize(hass, connection, msg)")
                self.assertEqual(len(first.body), 1)
                self.assertIsInstance(first.body[0], ast.Return)
                self.assertEqual(first.orelse, [])

    def test_every_allowlisted_music_assistant_command_is_classified(self):
        bridge = importlib.import_module(f"{PACKAGE}.command_bridge")
        self.assertEqual(
            set(AUTH.MUSIC_ASSISTANT_COMMAND_ACCESS), set(bridge.MUSIC_ASSISTANT_COMMAND_ALLOWLIST)
        )
        for command, level in AUTH.MUSIC_ASSISTANT_COMMAND_ACCESS.items():
            with self.subTest(command=command):
                self.assertIn(level, {READ, CONTROL, ADMIN})
                if command.startswith("players/cmd/") or command in {
                    "player_queues/play_media",
                    "player_queues/clear",
                    "player_queues/transfer",
                }:
                    self.assertEqual(level, CONTROL)
                if command.endswith("/library_items") or command in {"music/search", "players/all"}:
                    self.assertEqual(level, READ)
        self.assertEqual(AUTH.MUSIC_ASSISTANT_COMMAND_ACCESS["music/library/add_item"], ADMIN)
        self.assertEqual(AUTH.MUSIC_ASSISTANT_COMMAND_ACCESS["music/playlists/add_playlist_tracks"], ADMIN)
        self.assertEqual(AUTH.music_assistant_command_access("config/core/save"), "")
        self.assertEqual(AUTH.music_assistant_command_access("music/start_sync"), "")

    def test_http_and_service_tables_cover_every_endpoint(self):
        bridge = importlib.import_module(f"{PACKAGE}.command_bridge")
        self.assertEqual(set(AUTH.HTTP_COMMAND_ACCESS), set(bridge.HTTP_COMMAND_SCHEMAS))
        source = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
        services = set(re.findall(r'^SERVICE_[A-Z_]+ = "([a-z_]+)"$', source, re.M))
        services.add("set_interface_preferences")
        self.assertEqual(set(AUTH.SERVICE_ACCESS), services)
        self.assertEqual(AUTH.SERVICE_ACCESS["set_queue_settings"], ADMIN)

    def test_websocket_level_variants_depend_on_the_payload(self):
        level = AUTH.websocket_access_level
        self.assertEqual(level(PREFIX + "queue/settings", {}), READ)
        self.assertEqual(level(PREFIX + "queue/settings", {"values": {}}), ADMIN)
        self.assertEqual(level(PREFIX + "playlists", {}), READ)
        self.assertEqual(level(PREFIX + "playlists", {"action": "play"}), CONTROL)
        self.assertEqual(level(PREFIX + "playlists", {"action": "save"}), ADMIN)
        self.assertEqual(level(PREFIX + "playlists", {"action": "delete"}), ADMIN)
        self.assertEqual(level(PREFIX + "wheels/set", {"scope": "user"}), CONTROL)
        self.assertEqual(level(PREFIX + "wheels/set", {"scope": "global"}), ADMIN)
        self.assertEqual(level(PREFIX + "ma/command", {"command": "players/all"}), READ)
        self.assertEqual(level(PREFIX + "ma/command", {"command": "players/cmd/play"}), CONTROL)
        self.assertEqual(level(PREFIX + "ma/command", {"command": "config/core/save"}), "")
        self.assertEqual(level(PREFIX + "not/registered", {}), "")
        self.assertEqual(level("get_context", {}), READ)

    def test_targets_follow_the_runtime_key_precedence(self):
        targets = AUTH.payload_targets
        self.assertEqual(targets("player/command", {"entity_id": "a", "selected_player": "b"}), ["a"])
        self.assertEqual(targets("playback/play_media", {"selected_player": "b"}), ["b"])
        self.assertEqual(targets("player/command", {"player": "", "entity_id": " c "}), ["c"])
        self.assertEqual(
            targets("queue/transfer", {"source_entity_id": "a", "entity_id": "b"}), ["a", "b"]
        )
        self.assertEqual(
            targets("group/apply", {"owner": "a", "members": ["b", "a"], "remove_members": ["c"]}),
            ["a", "b", "c"],
        )
        self.assertEqual(targets("announce", {"player": "a", "players": ["b", "a"]}), ["a", "b"])
        self.assertEqual(targets("playlists", {"action": "play", "selected_player": "a"}), ["a"])
        self.assertEqual(targets("playlists", {"action": "list", "selected_player": "a"}), [])
        self.assertEqual(targets("schedules/set", {"entity_id": "a"}), ["a"])
        self.assertEqual(targets("set_timer", {"player": "a"}), ["a"])
        self.assertEqual(targets("transfer_queue", {"source_player": "a", "target_player": "b"}), ["a", "b"])
        self.assertEqual(targets("favorites/set", {"player": "a"}), [])
        self.assertEqual(targets("get_context", {"player": "a"}), [])
        self.assertEqual(
            targets(
                "ma/command",
                {
                    "command": "players/cmd/set_members",
                    "args": {
                        "target_player": "a",
                        "player_ids_to_add": ["b"],
                        "player_ids_to_remove": ["c", 5, ""],
                    },
                },
            ),
            ["a", "b", "c"],
        )
        self.assertEqual(
            targets("ma/command", {"args": {"source_queue_id": "a", "target_queue_id": "b"}}),
            ["a", "b"],
        )
        self.assertEqual(targets("ma/command", {"args": "not a dict"}), [])

    def test_stored_targets_resolve_ids_to_players_within_the_profile(self):
        schedules = [
            {"id": "s1", "profile_id": "default", "player": KITCHEN},
            {"id": "s1", "profile_id": "other", "player": BEDROOM},
        ]
        timers = [{"id": "t1", "player": BEDROOM}]
        rules = [{"player": KITCHEN}, {"player": BEDROOM, "profile_id": "default"}, {"player": "x", "profile_id": "other"}]
        stored = AUTH.stored_targets
        self.assertEqual(stored("schedules/delete", {"schedule_id": "s1"}, schedules=schedules), [KITCHEN])
        self.assertEqual(stored("run_schedule", {"id": "s1", "profile_id": "other"}, schedules=schedules), [BEDROOM])
        self.assertEqual(stored("schedules/run", {}, schedules=schedules), [])
        self.assertEqual(stored("timers/delete", {"timer_id": "t1"}, timers=timers), [BEDROOM])
        self.assertEqual(stored("delete_timer", {"id": "missing"}, timers=timers), [])
        self.assertEqual(stored("volume_rules/clear", {}, volume_rules=rules), [KITCHEN, BEDROOM])
        self.assertEqual(stored("clear_volume_rules", {"profile_id": "other"}, volume_rules=rules), ["x"])
        self.assertEqual(stored("volume_rules/set", {"player": KITCHEN}, volume_rules=rules), [])


class AccessDenialTests(TestCase):
    def denial(self, level, **kwargs):
        options = {"is_admin": False, "can_control": lambda entity_id: entity_id == KITCHEN}
        options.update(kwargs)
        return AUTH.access_denial(level, **options)

    def test_reads_are_open_to_every_authenticated_user(self):
        self.assertIsNone(self.denial(READ, can_control=lambda _e: False, targets=[BEDROOM]))

    def test_admin_level_requires_an_administrator(self):
        self.assertEqual(self.denial(ADMIN).reason, AUTH.ADMIN_REQUIRED)
        self.assertIsNone(self.denial(ADMIN, is_admin=True))

    def test_control_checks_every_target_entity(self):
        self.assertIsNone(self.denial(CONTROL, targets=[KITCHEN, KITCHEN]))
        denial = self.denial(CONTROL, targets=[KITCHEN, BEDROOM])
        self.assertEqual(denial.entity_id, BEDROOM)
        self.assertIn(BEDROOM, denial.reason)
        self.assertIsNone(self.denial(CONTROL, targets=[]))
        self.assertEqual(self.denial(CONTROL, targets=[], require_target=True).reason, AUTH.TARGET_REQUIRED)
        self.assertIsNone(self.denial(CONTROL, targets=[], require_target=True, is_admin=True))

    def test_manage_is_admin_only_unless_the_option_is_on(self):
        self.assertEqual(self.denial(MANAGE, targets=[KITCHEN]).reason, AUTH.MANAGEMENT_ADMIN_REQUIRED)
        self.assertIsNone(self.denial(MANAGE, targets=[KITCHEN], management_allowed=True))
        self.assertEqual(
            self.denial(MANAGE, targets=[BEDROOM], management_allowed=True).entity_id, BEDROOM
        )
        self.assertIsNone(self.denial(MANAGE, is_admin=True, targets=[BEDROOM]))

    def test_unknown_levels_are_refused_even_for_admins(self):
        self.assertIsNotNone(self.denial("", is_admin=True))
        self.assertIsNotNone(self.denial("owner", is_admin=True))
        self.assertIsNotNone(self.denial(None, is_admin=True))


class WebsocketReadTests(IsolatedAsyncioTestCase):
    async def test_read_commands_work_for_non_admin_and_read_only_users(self):
        for user in (FakeUser(), FakeUser(allowed=set())):
            for command, msg in READ_MESSAGES.items():
                with self.subTest(command=command, admin=user.is_admin, allowed=user.allowed):
                    connection, _runtime = await run(command, msg, user)
                    kind, value = connection.outcome
                    self.assertNotEqual((kind, value), ("error", ERR_UNAUTHORIZED))
                    self.assertEqual(kind, "result")
                    self.assertEqual(user.checked, [])

    async def test_queue_settings_reports_whether_the_user_may_edit(self):
        connection, _ = await run("queue/settings", {}, FakeUser())
        self.assertFalse(connection.send_result.call_args.args[1]["can_edit"])
        connection, _ = await run("queue/settings", {}, FakeUser(is_admin=True))
        self.assertTrue(connection.send_result.call_args.args[1]["can_edit"])


class WebsocketConfigurationWriteTests(IsolatedAsyncioTestCase):
    def all_writes(self):
        yield from ((c, m) for c, m in MANAGE_MESSAGES.items())
        yield from ((c, m) for c, m in ADMIN_MESSAGES.items())
        yield from ADMIN_MESSAGES_2

    async def test_non_admin_users_are_refused_every_configuration_write(self):
        for command, msg in self.all_writes():
            with self.subTest(command=command, msg=msg):
                user = FakeUser()
                connection, runtime = await run(command, msg, user)
                self.assertEqual(connection.outcome[0], "error")
                self.assertEqual(connection.send_error.call_args.args[1], ERR_UNAUTHORIZED)
                self.assertEqual(runtime.calls, [])
                self.assertFalse(runtime.async_save.called)
                self.assertFalse(runtime.artwork_lighting.configure.called)
                connection.send_result.assert_not_called()

    async def test_admins_succeed_at_every_configuration_write(self):
        for command, msg in self.all_writes():
            with self.subTest(command=command, msg=msg):
                runtime = FakeRuntime()
                runtime._storage = {"saved_playlists": {"default": {"p1": {"id": "p1", "name": "x", "uris": ["a://b"]}}}}
                connection, runtime = await run(command, msg, FakeUser(is_admin=True), runtime)
                kind, value = connection.outcome
                self.assertNotEqual((kind, value), ("error", ERR_UNAUTHORIZED))
                self.assertEqual(kind, "result", msg=str(connection.send_error.call_args))

    async def test_admin_entity_permissions_are_still_checked_for_management_writes(self):
        # An administrator with Home Assistant's admin policy can control every entity;
        # the check still runs so a restricted policy would apply.
        user = FakeUser(is_admin=True)
        connection, _ = await run("schedules/set", MANAGE_MESSAGES["schedules/set"], user)
        self.assertEqual(connection.outcome[0], "result")


class WebsocketPlaybackControlTests(IsolatedAsyncioTestCase):
    async def test_any_user_with_control_permission_may_run_playback_commands(self):
        for command, msg in CONTROL_MESSAGES.items():
            with self.subTest(command=command):
                runtime = FakeRuntime()
                runtime._storage = {"saved_playlists": {"default": {"p1": {"id": "p1", "name": "x", "uris": ["a://b"]}}}}
                user = FakeUser(allowed={KITCHEN})
                connection, runtime = await run(command, msg, user, runtime)
                kind, value = connection.outcome
                self.assertEqual(kind, "result", msg=str(connection.send_error.call_args))
                targets = AUTH.payload_targets(command, msg)
                self.assertEqual(
                    [entity for entity, _ in user.checked],
                    [runtime.control_entity_id(target) for target in targets],
                )

    async def test_users_without_control_permission_are_refused_on_that_player(self):
        cases = [
            ("player/command", {"player": BEDROOM, "command": "play"}),
            ("playback/play_media", {"entity_id": BEDROOM, "media_id": "library://track/1"}),
            ("queue/action", {"selected_player": BEDROOM, "action": "remove", "queue_item_id": "1"}),
            ("queue/transfer", {"source_player": KITCHEN, "target_player": BEDROOM}),
            ("queue/transfer", {"source_player": BEDROOM, "target_player": KITCHEN}),
            ("group/apply", {"owner": KITCHEN, "members": [BEDROOM]}),
            ("group/apply", {"owner": KITCHEN, "remove_members": [BEDROOM]}),
            ("group/apply", {"owner": BEDROOM, "clear_all": True}),
            ("announce", {"message": "Dinner", "player": KITCHEN, "players": [BEDROOM]}),
            ("playlists", {"action": "play", "playlist_id": "p1", "selected_player": BEDROOM}),
            ("ma/command", {"command": "players/cmd/play", "args": {"player_id": "ma-bedroom"}}),
            ("ma/command", {"command": "player_queues/clear", "args": {"queue_id": "queue-bedroom"}}),
            ("ma/command", {"command": "players/cmd/set_members", "args": {"target_player": "ma-kitchen", "player_ids_to_add": ["ma-bedroom"]}}),
            ("ma/command", {"command": "player_queues/transfer", "args": {"source_queue_id": "ma-kitchen", "target_queue_id": "queue-bedroom"}}),
            ("ma/command", {"command": "players/cmd/play", "args": {"player_id": "unknown-player"}}),
        ]
        for command, msg in cases:
            with self.subTest(command=command, msg=msg):
                user = FakeUser(allowed={KITCHEN})
                connection, runtime = await run(command, msg, user)
                self.assertEqual(connection.outcome, ("error", ERR_UNAUTHORIZED))
                self.assertIn("No permission to control", connection.send_error.call_args.args[2])
                self.assertEqual(runtime.calls, [])
                self.assertFalse(runtime.async_save.called)

    async def test_ma_control_commands_without_a_target_are_refused_for_non_admins(self):
        msg = {"command": "players/cmd/play", "args": {}}
        connection, runtime = await run("ma/command", msg, FakeUser())
        self.assertEqual(connection.outcome, ("error", ERR_UNAUTHORIZED))
        self.assertEqual(connection.send_error.call_args.args[2], AUTH.TARGET_REQUIRED)
        self.assertEqual(runtime.calls, [])
        connection, runtime = await run("ma/command", msg, FakeUser(is_admin=True))
        self.assertEqual(connection.outcome[0], "result")

    async def test_ma_reads_need_no_entity_permission(self):
        user = FakeUser(allowed=set())
        for command in ("players/all", "music/search", "player_queues/get_active_queue"):
            with self.subTest(command=command):
                connection, _ = await run(
                    "ma/command", {"command": command, "args": {"player_id": "ma-bedroom"}}, user
                )
                self.assertEqual(connection.outcome[0], "result")
        self.assertEqual(user.checked, [])

    async def test_denied_ma_commands_never_reach_the_runtime_for_admins_either(self):
        msg = {"command": "config/core/save", "args": {}}
        connection, runtime = await run("ma/command", msg, FakeUser(is_admin=True))
        self.assertEqual(connection.outcome, ("error", ERR_UNAUTHORIZED))
        self.assertEqual(runtime.calls, [])


class NonAdminManagementOptionTests(IsolatedAsyncioTestCase):
    def runtime(self):
        runtime = FakeRuntime(management_allowed=True)
        runtime.stored["schedules"] = [
            {"id": "s1", "profile_id": "default", "player": KITCHEN},
            {"id": "s2", "profile_id": "default", "player": BEDROOM},
        ]
        runtime.stored["timers"] = [{"id": "t1", "profile_id": "default", "player": BEDROOM}]
        runtime.stored["volume_rules"] = [
            {"profile_id": "default", "player": KITCHEN},
            {"profile_id": "default", "player": BEDROOM},
        ]
        return runtime

    async def test_option_allows_non_admin_users_to_manage_their_players(self):
        allowed = [
            ("schedules/set", {"player": KITCHEN, "time": "07:00"}),
            ("schedules/delete", {"schedule_id": "s1"}),
            ("schedules/run", {"schedule_id": "s1"}),
            ("timers/set", {"entity_id": KITCHEN, "minutes": 5}),
            ("timers/delete", {"player": KITCHEN}),
            ("volume_rules/set", {"player": KITCHEN, "max_volume": 30}),
            ("volume_rules/delete", {"player": KITCHEN}),
        ]
        for command, msg in allowed:
            with self.subTest(command=command):
                user = FakeUser(allowed={KITCHEN})
                connection, runtime = await run(command, msg, user, self.runtime())
                self.assertEqual(connection.outcome[0], "result")
                self.assertEqual(len(runtime.calls), 1)
                self.assertTrue(user.checked)

    async def test_option_still_requires_control_of_the_target_player(self):
        refused = [
            ("schedules/set", {"player": BEDROOM, "time": "07:00"}),
            ("schedules/delete", {"schedule_id": "s2"}),
            ("schedules/run", {"schedule_id": "s2"}),
            ("timers/set", {"player": BEDROOM, "minutes": 5}),
            ("timers/delete", {"timer_id": "t1"}),
            ("volume_rules/set", {"player": BEDROOM, "max_volume": 30}),
            ("volume_rules/delete", {"player": BEDROOM}),
            ("volume_rules/clear", {}),
        ]
        for command, msg in refused:
            with self.subTest(command=command):
                user = FakeUser(allowed={KITCHEN})
                connection, runtime = await run(command, msg, user, self.runtime())
                self.assertEqual(connection.outcome, ("error", ERR_UNAUTHORIZED))
                self.assertIn(BEDROOM, connection.send_error.call_args.args[2])
                self.assertEqual(runtime.calls, [])

    async def test_option_does_not_open_other_configuration(self):
        for command, msg in ADMIN_MESSAGES.items():
            with self.subTest(command=command):
                connection, runtime = await run(command, msg, FakeUser(), self.runtime())
                self.assertEqual(connection.outcome, ("error", ERR_UNAUTHORIZED))
                self.assertEqual(runtime.calls, [])

    async def test_option_off_refuses_even_users_who_control_the_player(self):
        connection, runtime = await run(
            "schedules/set", {"player": KITCHEN, "time": "07:00"}, FakeUser(allowed={KITCHEN})
        )
        self.assertEqual(connection.outcome, ("error", ERR_UNAUTHORIZED))
        self.assertEqual(connection.send_error.call_args.args[2], AUTH.MANAGEMENT_ADMIN_REQUIRED)
        self.assertEqual(runtime.calls, [])


class HTTPError(Exception):
    def __init__(self, text=""):
        super().__init__(text)
        self.text = text


class FakeRequest(dict):
    def __init__(self, body, user):
        super().__init__(hass_user=user)
        self._body = body

    async def json(self):
        return self._body


def load_command_view(runtime):
    source = COMPONENT / "__init__.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HomeiiFlowCommandView"]
    bridge = importlib.import_module(f"{PACKAGE}.command_bridge")
    ns: dict[str, Any] = {
        "vol": vol,
        "HomeAssistantView": object,
        "HomeAssistant": object,
        "web": SimpleNamespace(
            Request=object,
            Response=object,
            HTTPNotFound=type("NotFound", (HTTPError,), {}),
            HTTPBadRequest=type("BadRequest", (HTTPError,), {}),
            HTTPForbidden=type("Forbidden", (HTTPError,), {}),
            json_response=lambda result: {"json": result},
        ),
        "HTTP_COMMAND_SCHEMAS": bridge.HTTP_COMMAND_SCHEMAS,
        "music_assistant_command_allowed": bridge.music_assistant_command_allowed,
        "strip_internal_keys": bridge.strip_internal_keys,
        "async_get_runtime": lambda _hass: runtime,
        "CONF_INSTANCE_ID": "instance_id",
        "CONF_PROFILE_ID": "profile_id",
        "POLICY_CONTROL": POLICY_CONTROL,
        "access_denial": AUTH.access_denial,
        "http_access_level": AUTH.http_access_level,
        "payload_targets": AUTH.payload_targets,
        "requires_target": AUTH.requires_target,
    }
    exec(
        compile(ast.fix_missing_locations(ast.Module(body=[FUTURE, *nodes], type_ignores=[])), str(source), "exec"),
        ns,
    )
    return ns["HomeiiFlowCommandView"](hass=None), ns["web"].HTTPForbidden


class CommandViewAuthorizationTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = FakeRuntime()
        self.view, self.forbidden = load_command_view(self.runtime)

    async def post(self, command, body, user):
        return await self.view.post(FakeRequest(body, user), command)

    async def test_reads_work_for_read_only_users(self):
        user = FakeUser(allowed=set())
        for command in ("get_context", "bootstrap/get", "queue/get", "library/get", "favorites/get", "search/get"):
            with self.subTest(command=command):
                result = await self.post(command, {}, user)
                self.assertIn("json", result)
        result = await self.post("ma/command", {"command": "players/all"}, user)
        self.assertIn("json", result)
        self.assertEqual(user.checked, [])

    async def test_favorites_set_is_open_to_any_authenticated_user(self):
        result = await self.post("favorites/set", {"favorite": True, "uri": "x"}, FakeUser(allowed=set()))
        self.assertIn("json", result)

    async def test_control_commands_check_entity_permissions(self):
        body = {"command": "players/cmd/play", "args": {"player_id": "ma-bedroom"}}
        with self.assertRaises(self.forbidden) as caught:
            await self.post("ma/command", body, FakeUser(allowed={KITCHEN}))
        self.assertIn(BEDROOM, caught.exception.text)
        self.assertEqual(self.runtime.calls, [])
        result = await self.post("ma/command", body, FakeUser(allowed={BEDROOM}))
        self.assertIn("json", result)
        self.assertEqual(self.runtime.calls[0][0], "async_music_assistant_command")

    async def test_library_writes_and_targetless_control_are_refused_for_non_admins(self):
        cases = [
            {"command": "music/library/add_item", "args": {"item": "x"}},
            {"command": "music/playlists/add_playlist_tracks", "args": {"db_playlist_id": "1"}},
            {"command": "players/cmd/play", "args": {}},
        ]
        for body in cases:
            with self.subTest(body=body):
                with self.assertRaises(self.forbidden):
                    await self.post("ma/command", body, FakeUser())
                result = await self.post("ma/command", body, FakeUser(is_admin=True))
                self.assertIn("json", result)

    async def test_denied_bridge_commands_stay_forbidden_for_admins(self):
        with self.assertRaises(self.forbidden) as caught:
            await self.post("ma/command", {"command": "config/core/save"}, FakeUser(is_admin=True))
        self.assertIn("not allowed", caught.exception.text)
        self.assertEqual(self.runtime.calls, [])


class FakeUnauthorized(Exception):
    def __init__(self, **kwargs):
        super().__init__(str(kwargs))
        self.kwargs = kwargs


class FakeUnknownUser(FakeUnauthorized):
    pass


def load_service_guard(runtime):
    source = COMPONENT / "__init__.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    wanted = {"_async_check_service_access", "_async_register_guarded_service"}
    nodes = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and n.name in wanted]
    ns: dict[str, Any] = {
        "Any": Any,
        "vol": vol,
        "HomeAssistant": object,
        "ServiceCall": object,
        "DOMAIN": "maverick_music_flow",
        "Unauthorized": FakeUnauthorized,
        "UnknownUser": FakeUnknownUser,
        "POLICY_CONTROL": POLICY_CONTROL,
        "async_get_runtime": lambda _hass: runtime,
        "CONF_PROFILE_ID": "profile_id",
        "DEFAULT_PROFILE_ID": "default",
        "ACCESS_MANAGE": MANAGE,
        "access_denial": AUTH.access_denial,
        "payload_targets": AUTH.payload_targets,
        "requires_target": AUTH.requires_target,
        "service_access_level": AUTH.service_access_level,
        "stored_targets": AUTH.stored_targets,
    }
    exec(
        compile(ast.fix_missing_locations(ast.Module(body=[FUTURE, *nodes], type_ignores=[])), str(source), "exec"),
        ns,
    )
    return ns["_async_check_service_access"], ns["_async_register_guarded_service"]


class ServiceGuardTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = FakeRuntime()
        self.runtime.stored["schedules"] = [{"id": "s1", "profile_id": "default", "player": BEDROOM}]
        self.check, self.register = load_service_guard(self.runtime)
        self.users: dict[str, FakeUser] = {}
        self.hass = SimpleNamespace(
            auth=SimpleNamespace(async_get_user=AsyncMock(side_effect=lambda user_id: self.users.get(user_id))),
            services=SimpleNamespace(has_service=lambda *_a: False, async_register=Mock()),
        )

    def call(self, user_id, **data):
        return SimpleNamespace(context=SimpleNamespace(user_id=user_id), data=data)

    def user(self, **kwargs):
        user = FakeUser(**kwargs)
        self.users[user.id] = user
        return user

    async def test_calls_without_a_user_context_always_pass(self):
        for service in AUTH.SERVICE_ACCESS:
            with self.subTest(service=service):
                await self.check(self.hass, service, self.call(None, player=BEDROOM))
        self.hass.auth.async_get_user.assert_not_awaited()

    async def test_unknown_users_are_refused(self):
        with self.assertRaises(FakeUnknownUser):
            await self.check(self.hass, "play_media", self.call("ghost", player=KITCHEN))

    async def test_non_admin_users_are_refused_management_and_configuration_services(self):
        user = self.user()
        for service in ("set_schedule", "delete_schedule", "run_schedule", "set_timer", "delete_timer",
                        "set_volume_rule", "delete_volume_rule", "clear_volume_rules",
                        "set_screensaver", "run_orchestration", "set_interface_preferences", "set_queue_settings"):
            with self.subTest(service=service):
                with self.assertRaises(FakeUnauthorized) as caught:
                    await self.check(self.hass, service, self.call(user.id, player=KITCHEN))
                self.assertEqual(set(caught.exception.kwargs), {"context"})
        admin = self.user(is_admin=True)
        for service in AUTH.SERVICE_ACCESS:
            with self.subTest(service=service, admin=True):
                await self.check(self.hass, service, self.call(admin.id, player=KITCHEN))

    async def test_playback_services_check_the_target_entities(self):
        user = self.user(allowed={KITCHEN})
        await self.check(self.hass, "play_media", self.call(user.id, player=KITCHEN, media_id="x"))
        await self.check(self.hass, "announce", self.call(user.id, message="hi", players=[KITCHEN]))
        await self.check(self.hass, "show_screensaver", self.call(user.id))
        with self.assertRaises(FakeUnauthorized) as caught:
            await self.check(self.hass, "player_command", self.call(user.id, player=BEDROOM, command="play"))
        self.assertEqual(caught.exception.kwargs["entity_id"], BEDROOM)
        self.assertEqual(caught.exception.kwargs["permission"], POLICY_CONTROL)
        with self.assertRaises(FakeUnauthorized):
            await self.check(self.hass, "transfer_queue", self.call(user.id, source_player=KITCHEN, target_player=BEDROOM))

    async def test_option_lets_non_admins_manage_players_they_control(self):
        self.runtime.management_allowed = True
        user = self.user(allowed={KITCHEN})
        await self.check(self.hass, "set_schedule", self.call(user.id, player=KITCHEN, time="07:00"))
        with self.assertRaises(FakeUnauthorized) as caught:
            await self.check(self.hass, "run_schedule", self.call(user.id, id="s1"))
        self.assertEqual(caught.exception.kwargs["entity_id"], BEDROOM)
        with self.assertRaises(FakeUnauthorized):
            await self.check(self.hass, "set_screensaver", self.call(user.id, enabled=True))

    async def test_unclassified_services_are_refused_for_everyone_with_a_user(self):
        admin = self.user(is_admin=True)
        with self.assertRaises(FakeUnauthorized):
            await self.check(self.hass, "not_a_service", self.call(admin.id))

    async def test_guarded_registration_runs_the_check_before_the_handler(self):
        handler = AsyncMock()
        self.register(self.hass, "play_media", handler, schema="schema")
        name, args, kwargs = self.hass.services.async_register.call_args_list[0][0][0], self.hass.services.async_register.call_args.args, self.hass.services.async_register.call_args.kwargs
        self.assertEqual(args[:2], ("maverick_music_flow", "play_media"))
        self.assertEqual(kwargs, {"schema": "schema"})
        guarded = args[2]
        user = self.user(allowed={KITCHEN})
        with self.assertRaises(FakeUnauthorized):
            await guarded(self.call(user.id, player=BEDROOM, media_id="x"))
        handler.assert_not_awaited()
        call = self.call(user.id, player=KITCHEN, media_id="x")
        await guarded(call)
        handler.assert_awaited_once_with(call)
        self.hass.services.has_service = lambda *_a: True
        self.register(self.hass, "play_media", handler)
        self.assertEqual(self.hass.services.async_register.call_count, 1)


def load_runtime_helpers():
    source = COMPONENT / "runtime.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HomeiiFlowRuntime")
    entry = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "EngineEntry")
    cls.decorator_list = []
    wanted = {"control_entity_id", "non_admin_management_allowed", "_matching_entry", "register_entry", "unregister_entry"}
    cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name in wanted]
    ns: dict[str, Any] = {
        "Any": Any,
        "_clean_string": lambda value: str(value or "").strip(),
        "_normalized_http_url": lambda value: value,
        "DEFAULT_INSTANCE_ID": "default",
        "DEFAULT_PROFILE_ID": "default",
        "dataclass": dataclass,
        "field": field,
        "_utc_iso": lambda: "now",
    }
    exec(
        compile(ast.fix_missing_locations(ast.Module(body=[FUTURE, entry, cls], type_ignores=[])), str(source), "exec"),
        ns,
    )
    return ns["HomeiiFlowRuntime"], ns["EngineEntry"]


class RuntimeHelperTests(TestCase):
    def runtime(self):
        Runtime, Entry = load_runtime_helpers()
        runtime = Runtime()
        runtime._entries = {}
        runtime._ma_players_by_id = {"ma-kitchen": {"entity_id": KITCHEN, "raw_player_id": "ma-kitchen"}}
        runtime._ma_players_by_entity = {
            KITCHEN: {"entity_id": KITCHEN, "raw_player_id": "ma-kitchen", "active_queue": "queue-kitchen", "mass_player_id": "ma-kitchen"},
            BEDROOM: {"entity_id": BEDROOM, "raw_player_id": "ma-bedroom", "active_queue": "queue-kitchen"},
        }
        runtime._ha_entity_for_ma_player = lambda raw: f"media_player.homeii_{raw['player_id']}"
        runtime._refresh_music_assistant_connection = Mock()
        runtime._schedule_media_cache_warm = Mock()
        runtime._schedule_music_assistant_health_probe = Mock()
        runtime._music_assistant_client = SimpleNamespace(configure=Mock())
        return runtime, Entry

    def test_control_entity_id_maps_entities_players_queues_and_unknowns(self):
        runtime, _ = self.runtime()
        self.assertEqual(runtime.control_entity_id(KITCHEN), KITCHEN)
        self.assertEqual(runtime.control_entity_id(" media_player.other "), "media_player.other")
        self.assertEqual(runtime.control_entity_id("ma-kitchen"), KITCHEN)
        self.assertEqual(runtime.control_entity_id("ma-bedroom"), BEDROOM)
        self.assertEqual(runtime.control_entity_id("queue-kitchen"), KITCHEN)
        self.assertEqual(runtime.control_entity_id("mystery"), "media_player.homeii_mystery")
        self.assertEqual(runtime.control_entity_id(""), "")
        self.assertEqual(runtime.control_entity_id(None), "")

    def test_non_admin_management_option_follows_the_matching_entry(self):
        runtime, Entry = self.runtime()
        self.assertFalse(runtime.non_admin_management_allowed())
        runtime.register_entry("one", instance_id="main", profile_id="default", title="t", enable_experimental=False)
        self.assertFalse(runtime.non_admin_management_allowed())
        self.assertFalse(runtime.non_admin_management_allowed("main"))
        runtime.register_entry("two", instance_id="kids", profile_id="default", title="t", enable_experimental=False, allow_non_admin_management=True)
        self.assertTrue(runtime.non_admin_management_allowed("kids"))
        self.assertFalse(runtime.non_admin_management_allowed("main"))
        self.assertFalse(runtime.non_admin_management_allowed())
        self.assertIn("allow_non_admin_management", runtime._entries["two"].as_dict())
        self.assertTrue(runtime._entries["two"].as_dict()["allow_non_admin_management"])


class WiringTests(TestCase):
    def test_options_flow_and_setup_entry_carry_the_option(self):
        config_flow = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")
        tree = ast.parse(config_flow)
        general = next(
            n for cls in tree.body if isinstance(cls, ast.ClassDef) and cls.name == "HomeiiFlowOptionsFlow"
            for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_step_general"
        )
        self.assertIn("CONF_ALLOW_NON_ADMIN_MANAGEMENT", ast.unparse(general))
        init = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("allow_non_admin_management=allow_non_admin_management", init)
        self.assertIn("entry.options.get(CONF_ALLOW_NON_ADMIN_MANAGEMENT, False)", init)
        self.assertIn("async_register_admin_service(hass, DOMAIN, SERVICE_SET_QUEUE_SETTINGS", init)
        self.assertNotIn("hass.services.async_register(\n            DOMAIN,", init)
        const = (COMPONENT / "const.py").read_text(encoding="utf-8")
        self.assertIn('CONF_ALLOW_NON_ADMIN_MANAGEMENT = "allow_non_admin_management"', const)
        for name in ("strings.json", "translations/en.json"):
            with self.subTest(file=name):
                text = (COMPONENT / name).read_text(encoding="utf-8")
                self.assertIn("Allow non-admin users to manage schedules, timers and volume rules", text)

    def test_readme_documents_the_permission_model(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("**Permission model.**", readme)
        self.assertIn("Allow non-admin users to manage schedules, timers and volume rules", readme)


if __name__ == "__main__":
    main()
