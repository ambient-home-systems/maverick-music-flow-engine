"""Shared fixtures for the Home Assistant tests in tests/ha/.

The tests in tests/ha/ load the real integration into a Home Assistant test instance
(pytest-homeassistant-custom-component). Three fixtures do the heavy lifting:

- ``fake_ma``: an aiohttp server on 127.0.0.1 that speaks enough of the Music Assistant
  server API for the Engine: ``GET /info``, ``POST /api``, the ``/ws`` WebSocket
  (greeting, ``auth``, the onboarding ``auth/*`` commands and every command the Engine
  sends) and ``/imageproxy`` artwork.
- ``mock_music_assistant``: stands in for Home Assistant's ``music_assistant``
  integration, which the Engine declares as a dependency. It provides a loaded config
  entry, two ``media_player`` entities registered on the ``music_assistant`` platform
  and recorded ``music_assistant``/``media_player`` services.
- ``loaded_entry``: an Engine config entry that is set up and connected to ``fake_ma``.

The older function-level unittest files in tests/ do not use these fixtures; pytest
collects them alongside the Home Assistant tests. tests/ha/ has no ``__init__.py`` on
purpose: ``python -m unittest discover -s tests`` then skips it, so the older tests still
run where Home Assistant is not installed.
"""

from __future__ import annotations

import asyncio
import copy
import io
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.setup import async_setup_component
from PIL import Image
from pytest_homeassistant_custom_component.common import (
    CLIENT_ID,
    MockConfigEntry,
    MockModule,
    MockUser,
    async_mock_service,
    mock_integration,
)

from custom_components.maverick_music_flow.const import (
    CONF_ENABLE_EXPERIMENTAL,
    CONF_INSTANCE_ID,
    CONF_MUSIC_ASSISTANT_EXTERNAL_URL,
    CONF_MUSIC_ASSISTANT_TOKEN,
    CONF_MUSIC_ASSISTANT_URL,
    CONF_PROFILE_ID,
    CONFIG_ENTRY_VERSION,
    DEFAULT_INSTANCE_ID,
    DEFAULT_NAME,
    DEFAULT_PROFILE_ID,
    DOMAIN,
    MUSIC_ASSISTANT_SCHEMA_VALIDATED,
    SIGNAL_ENGINE_UPDATED,
)

MA_TOKEN = "engine-ma-token"
MA_USERNAME = "admin"
MA_PASSWORD = "correct-horse"
MA_SERVER_ID = "fake-music-assistant"

KITCHEN = "media_player.kitchen"
BEDROOM = "media_player.bedroom"
KITCHEN_ID = "ma_kitchen"
BEDROOM_ID = "ma_bedroom"


def png_bytes(color: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    """Return a small valid PNG image."""
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buffer, format="PNG")
    return buffer.getvalue()


async def async_wait_for(
    condition: Callable[[], Any], *, timeout: float = 5, message: str = "condition"
) -> None:
    """Wait until ``condition()`` is truthy.

    The Engine's Music Assistant connection runs in its own task over a real socket,
    which ``hass.async_block_till_done()`` does not wait for.
    """
    try:
        async with asyncio.timeout(timeout):
            while not condition():
                await asyncio.sleep(0.01)
    except TimeoutError:
        pytest.fail(f"Timed out waiting for {message}")


@dataclass
class FakeConnection:
    """One WebSocket client connected to the fake Music Assistant server."""

    ws: web.WebSocketResponse
    token: str = ""


@dataclass
class FakeMusicAssistant:
    """In-process Music Assistant server for the Engine to talk to.

    ``commands`` records every command received on ``POST /api`` or, after
    authentication, on the WebSocket, as ``(command, args)``. Set ``responses[command]``
    to a value, or a callable taking ``args``, to override the default result. A callable
    may raise :class:`FakeCommandError` to answer with a Music Assistant error.
    """

    url: str = ""
    schema_version: int = MUSIC_ASSISTANT_SCHEMA_VALIDATED
    server_version: str = "2.10.0"
    valid_tokens: set[str] = field(default_factory=lambda: {MA_TOKEN})
    commands: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    responses: dict[str, Any] = field(default_factory=dict)
    connections: list[FakeConnection] = field(default_factory=list)
    login_tokens: list[str] = field(default_factory=list)
    created_tokens: list[str] = field(default_factory=list)
    revoked_tokens: list[str] = field(default_factory=list)
    image_requests: list[dict[str, Any]] = field(default_factory=list)
    players: list[dict[str, Any]] = field(default_factory=list)
    queues: dict[str, dict[str, Any]] = field(default_factory=dict)
    queue_items: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    library: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    queue_config: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {
            "autoplay_enabled": {"key": "autoplay_enabled", "type": "boolean", "value": True},
            "crossfade_enabled": {"key": "crossfade_enabled", "type": "boolean", "value": False},
            "crossfade_duration": {
                "key": "crossfade_duration",
                "type": "integer",
                "value": 8,
                "range": [1, 15],
            },
        }
    )

    def __post_init__(self) -> None:
        """Create the artwork image that /imageproxy serves."""
        self.image = png_bytes()

    def seed(self) -> None:
        """Create the default players, queues and library once the URL is known."""
        artwork = f"{self.url}/imageproxy?provider=builtin&path=cover1.png&size=512"
        track = {
            "item_id": "1",
            "provider": "library",
            "name": "Song One",
            "uri": "library://track/1",
            "media_type": "track",
            "duration": 200,
            "artists": [{"item_id": "1", "provider": "library", "name": "Artist One"}],
            "album": {"item_id": "1", "provider": "library", "name": "Album One"},
            "image": artwork,
            "favorite": False,
        }
        self.players = [
            {
                "player_id": KITCHEN_ID,
                "name": "Kitchen",
                "type": "player",
                "available": True,
                "powered": True,
                "playback_state": "playing",
                "volume_level": 30,
                "volume_muted": False,
                "active_source": KITCHEN_ID,
                "group_members": [],
                "current_media": {
                    "uri": "library://track/1",
                    "media_type": "track",
                    "title": "Song One",
                    "artist": "Artist One",
                    "album": "Album One",
                    "image_url": artwork,
                    "duration": 200,
                },
                "elapsed_time": 12,
                "elapsed_time_last_updated": time.time(),
            },
            {
                "player_id": BEDROOM_ID,
                "name": "Bedroom",
                "type": "player",
                "available": True,
                "powered": True,
                "playback_state": "idle",
                "volume_level": 80,
                "volume_muted": False,
                "active_source": BEDROOM_ID,
                "group_members": [],
                "current_media": None,
            },
        ]
        current_item = {
            "queue_item_id": "qi-1",
            "queue_id": KITCHEN_ID,
            "name": "Artist One - Song One",
            "duration": 200,
            "media_item": track,
            "image": artwork,
        }
        self.queues = {
            KITCHEN_ID: {
                "queue_id": KITCHEN_ID,
                "active": True,
                "display_name": "Kitchen",
                "available": True,
                "items": 2,
                "shuffle_enabled": False,
                "repeat_mode": "off",
                "current_index": 0,
                "elapsed_time": 12,
                "state": "playing",
                "current_item": current_item,
                "next_item": None,
            },
            BEDROOM_ID: {
                "queue_id": BEDROOM_ID,
                "active": False,
                "display_name": "Bedroom",
                "available": True,
                "items": 0,
                "shuffle_enabled": False,
                "repeat_mode": "off",
                "current_index": None,
                "state": "idle",
                "current_item": None,
                "next_item": None,
            },
        }
        self.queue_items = {
            KITCHEN_ID: [
                current_item,
                {
                    "queue_item_id": "qi-2",
                    "queue_id": KITCHEN_ID,
                    "name": "Artist One - Song Two",
                    "duration": 180,
                    "media_item": {
                        **track,
                        "item_id": "2",
                        "name": "Song Two",
                        "uri": "library://track/2",
                    },
                },
            ],
            BEDROOM_ID: [],
        }
        self.library = {
            "playlists": [
                {
                    "item_id": "1",
                    "provider": "library",
                    "name": "Morning Mix",
                    "uri": "library://playlist/1",
                    "media_type": "playlist",
                    "image": artwork,
                    "favorite": True,
                }
            ],
            "tracks": [track],
            "albums": [],
            "artists": [],
            "radios": [],
            "podcasts": [],
            "audiobooks": [],
            "genres": [],
        }

    # --- helpers for tests -------------------------------------------------------------

    def commands_named(self, command: str) -> list[dict[str, Any]]:
        """Return the args of every received command with this name."""
        return [args for name, args in self.commands if name == command]

    @property
    def authenticated_connections(self) -> list[FakeConnection]:
        """Return connections that authenticated with a valid token."""
        return [conn for conn in self.connections if conn.token and not conn.ws.closed]

    async def async_send_event(self, event: str, object_id: str = "", data: Any = None) -> None:
        """Push a Music Assistant event to every authenticated connection."""
        for conn in self.authenticated_connections:
            await conn.ws.send_json({"event": event, "object_id": object_id, "data": data})

    async def async_drop_connections(self) -> None:
        """Close every WebSocket connection, as a restarting server would."""
        for conn in list(self.connections):
            await conn.ws.close()

    # --- protocol ----------------------------------------------------------------------

    def info(self) -> dict[str, Any]:
        """Return the server info shared by GET /info and the WebSocket greeting."""
        return {
            "server_id": MA_SERVER_ID,
            "server_version": self.server_version,
            "schema_version": self.schema_version,
            "min_supported_schema_version": 24,
            "base_url": self.url,
            "homeassistant_addon": False,
            "onboard_done": True,
        }

    def default_result(self, command: str, args: dict[str, Any]) -> Any:
        """Return the result a real server would send for an Engine command."""
        if command == "players/all":
            return self.players
        if command == "player_queues/all":
            return list(self.queues.values())
        if command == "player_queues/get_active_queue":
            player_id = str(args.get("player_id") or "")
            return self.queues.get(player_id) or {"queue_id": player_id, "items": 0}
        if command == "player_queues/get":
            return self.queues.get(str(args.get("queue_id") or ""))
        if command == "player_queues/items":
            items = self.queue_items.get(str(args.get("queue_id") or ""), [])
            offset = int(args.get("offset") or 0)
            limit = int(args.get("limit") or 500)
            return items[offset : offset + limit]
        if command == "music/search":
            return {
                "tracks": self.library["tracks"],
                "artists": [],
                "albums": [],
                "playlists": self.library["playlists"],
                "radio": [],
                "podcasts": [],
                "audiobooks": [],
            }
        if command.startswith("music/") and command.endswith("/library_items"):
            root = command.split("/")[1]
            items = self.library.get(root, [])
            if args.get("favorite"):
                items = [item for item in items if item.get("favorite")]
            offset = int(args.get("offset") or 0)
            limit = int(args.get("limit") or 500)
            return items[offset : offset + limit]
        if command == "music/item_by_uri":
            return self.library["tracks"][0]
        if command == "config/core/get":
            return {"values": copy.deepcopy(self.queue_config)}
        if command == "config/core/save":
            for key, value in (args.get("values") or {}).items():
                self.queue_config[key]["value"] = value
            return None
        if command == "player_queues/play_media":
            # Start a new queue item, so the Engine's playback verification succeeds.
            queue = self.queues.setdefault(
                str(args.get("queue_id") or ""), {"queue_id": str(args.get("queue_id") or "")}
            )
            if args.get("option") in (None, "play", "replace"):
                played = len(self.commands_named("player_queues/play_media"))
                queue["current_item"] = {
                    "queue_item_id": f"qi-played-{played}",
                    "queue_id": queue["queue_id"],
                    "name": str(args.get("media")),
                }
                queue["state"] = "playing"
            return None
        if command in {"players/cmd/play", "players/cmd/pause", "players/cmd/stop"}:
            state = {"play": "playing", "pause": "paused", "stop": "idle"}[
                command.rsplit("/", 1)[1]
            ]
            for player in self.players:
                if player["player_id"] == args.get("player_id"):
                    player["playback_state"] = state
            return None
        return None

    def handle_command(self, command: str, args: dict[str, Any]) -> Any:
        """Record a command and return its result."""
        self.commands.append((command, args))
        if command in self.responses:
            response = self.responses[command]
            return response(args) if callable(response) else response
        return self.default_result(command, args)

    # --- HTTP handlers -----------------------------------------------------------------

    async def handle_info(self, request: web.Request) -> web.Response:
        """GET /info."""
        return web.json_response(self.info())

    async def handle_api(self, request: web.Request) -> web.Response:
        """POST /api: authenticated JSON-RPC style command endpoint."""
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth.removeprefix("Bearer ") not in self.valid_tokens:
            return web.json_response(
                {"error_code": 20, "details": "Authentication required"}, status=401
            )
        body = await request.json()
        try:
            result = self.handle_command(str(body.get("command") or ""), body.get("args") or {})
        except FakeCommandError as err:
            return web.json_response({"error_code": 999, "details": str(err)}, status=400)
        return web.json_response(result)

    async def handle_imageproxy(self, request: web.Request) -> web.StreamResponse:
        """GET /imageproxy: artwork that, like MA's, needs the bearer token."""
        auth = request.headers.get("Authorization", "")
        self.image_requests.append({"path": request.path_qs, "authorization": auth})
        if auth.removeprefix("Bearer ") not in self.valid_tokens:
            return web.Response(status=401)
        return web.Response(body=self.image, content_type="image/png")

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """GET /ws: greeting, authentication and commands."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        conn = FakeConnection(ws)
        self.connections.append(conn)
        await ws.send_json(self.info())
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            data = message.json()
            message_id = data.get("message_id")
            command = str(data.get("command") or "")
            args = data.get("args") or {}
            try:
                result = await self._ws_command(conn, command, args)
            except FakeCommandError as err:
                await ws.send_json(
                    {"message_id": message_id, "error_code": err.code, "details": str(err)}
                )
                continue
            if isinstance(result, _Logout):
                await ws.send_json({"message_id": message_id, "result": True})
                await ws.close()
                break
            if isinstance(result, list) and len(result) > 2:
                # Large lists arrive in chunks marked "partial", like MA sends them.
                await ws.send_json(
                    {"message_id": message_id, "result": result[:2], "partial": True}
                )
                result = result[2:]
            await ws.send_json({"message_id": message_id, "result": result})
        return ws

    async def _ws_command(self, conn: FakeConnection, command: str, args: dict[str, Any]) -> Any:
        """Handle one WebSocket command."""
        if command == "auth/login":
            if (
                args.get("username") != MA_USERNAME
                or args.get("password") != MA_PASSWORD
                or args.get("provider_id") != "builtin"
            ):
                raise FakeCommandError("Invalid username or password", code=20)
            login_token = f"login-token-{len(self.login_tokens) + 1}"
            self.login_tokens.append(login_token)
            self.valid_tokens.add(login_token)
            return {"success": True, "access_token": login_token, "user": {"username": MA_USERNAME}}
        if command == "auth":
            token = str(args.get("token") or "")
            if token not in self.valid_tokens:
                raise FakeCommandError("Invalid token", code=20)
            conn.token = token
            return {"authenticated": True, "user": {"username": MA_USERNAME}}
        if not conn.token:
            raise FakeCommandError("Authentication required", code=20)
        if command == "auth/token/create":
            token = f"created-token-{len(self.created_tokens) + 1}"
            self.created_tokens.append(token)
            self.valid_tokens.add(token)
            return token
        if command == "auth/logout":
            self.revoked_tokens.append(conn.token)
            self.valid_tokens.discard(conn.token)
            return _Logout()
        return self.handle_command(command, args)

    def app(self) -> web.Application:
        """Return the aiohttp application."""
        app = web.Application()
        app.router.add_get("/info", self.handle_info)
        app.router.add_post("/api", self.handle_api)
        app.router.add_get("/ws", self.handle_ws)
        app.router.add_get("/imageproxy", self.handle_imageproxy)
        app.router.add_get("/imageproxy/{image_id}", self.handle_imageproxy)
        return app


class FakeCommandError(Exception):
    """Raised by a fake command handler to answer with a Music Assistant error."""

    def __init__(self, message: str, *, code: int = 999) -> None:
        """Store the Music Assistant error code."""
        super().__init__(message)
        self.code = code


class _Logout:
    """Marker result: reply, then drop the connection like MA does after auth/logout."""


@pytest.fixture
async def fake_ma(socket_enabled: None) -> AsyncGenerator[FakeMusicAssistant]:
    """Run a fake Music Assistant server on 127.0.0.1."""
    server_state = FakeMusicAssistant()
    server = TestServer(server_state.app(), host="127.0.0.1")
    await server.start_server()
    server_state.url = str(server.make_url("")).rstrip("/")
    server_state.seed()
    yield server_state
    # Close WebSockets first: the server otherwise waits up to 60 seconds for their
    # handlers when an Engine entry is still connected at teardown.
    await server_state.async_drop_connections()
    await server.close()


@dataclass
class MusicAssistantStub:
    """What the stub music_assistant integration provides."""

    config_entry: MockConfigEntry
    service_calls: dict[str, list[ServiceCall]]


@pytest.fixture
async def mock_music_assistant(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    entity_registry: er.EntityRegistry,
    fake_ma: FakeMusicAssistant,
) -> MusicAssistantStub:
    """Stand in for Home Assistant's music_assistant integration.

    The real integration needs the music-assistant-client library and a server; the
    Engine only needs it to be present, loaded, and to own the media_player entities.
    """
    # A real installation always runs the http integration (the Engine registers views
    # on it) even though the Engine's manifest does not list it as a dependency.
    assert await async_setup_component(hass, "http", {})
    mock_integration(hass, MockModule("music_assistant"))
    ma_entry = MockConfigEntry(
        domain="music_assistant",
        title="Music Assistant",
        data={"url": fake_ma.url},
        unique_id=MA_SERVER_ID,
    )
    ma_entry.add_to_hass(hass)
    ma_entry.mock_state(hass, ConfigEntryState.LOADED)
    for entity_id, player_id, name, state, volume in (
        (KITCHEN, KITCHEN_ID, "Kitchen", "playing", 0.3),
        (BEDROOM, BEDROOM_ID, "Bedroom", "idle", 0.8),
    ):
        entity_registry.async_get_or_create(
            "media_player",
            "music_assistant",
            player_id,
            suggested_object_id=entity_id.split(".", 1)[1],
            config_entry=ma_entry,
        )
        hass.states.async_set(
            entity_id,
            state,
            {
                "friendly_name": name,
                "volume_level": volume,
                "is_volume_muted": False,
                "active_queue": player_id,
                "mass_player_type": "player",
            },
        )
    service_calls = {
        f"{domain}.{service}": async_mock_service(hass, domain, service)
        for domain, service in (
            ("music_assistant", "play_announcement"),
            ("media_player", "volume_set"),
            ("media_player", "media_stop"),
            ("media_player", "media_pause"),
            ("media_player", "play_media"),
        )
    }
    return MusicAssistantStub(ma_entry, service_calls)


@pytest.fixture
def kitchen_user(hass: HomeAssistant) -> MockUser:
    """Return a non-admin user who may control the kitchen player and nothing else."""
    user = MockUser(name="Kitchen user").add_to_hass(hass)
    user.mock_policy({"entities": {"entity_ids": {KITCHEN: True}}})
    return user


@pytest.fixture
async def kitchen_user_token(hass: HomeAssistant, kitchen_user: MockUser) -> str:
    """Return an access token for ``kitchen_user``."""
    refresh_token = await hass.auth.async_create_refresh_token(kitchen_user, CLIENT_ID)
    return hass.auth.async_create_access_token(refresh_token)


@pytest.fixture
def entry_options() -> dict[str, Any]:
    """Options for the Engine config entry; override with ``pytest.mark.parametrize``."""
    return {CONF_ENABLE_EXPERIMENTAL: False}


@pytest.fixture
def config_entry(fake_ma: FakeMusicAssistant, entry_options: dict[str, Any]) -> MockConfigEntry:
    """Return an Engine config entry pointing at the fake server (not yet added)."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=DEFAULT_NAME,
        version=CONFIG_ENTRY_VERSION,
        unique_id=DEFAULT_INSTANCE_ID,
        data={
            CONF_INSTANCE_ID: DEFAULT_INSTANCE_ID,
            CONF_PROFILE_ID: DEFAULT_PROFILE_ID,
            CONF_MUSIC_ASSISTANT_URL: fake_ma.url,
            CONF_MUSIC_ASSISTANT_EXTERNAL_URL: "",
            CONF_MUSIC_ASSISTANT_TOKEN: MA_TOKEN,
        },
        options=entry_options,
    )


def engine_runtime(hass: HomeAssistant) -> Any:
    """Return the Engine's shared runtime."""
    return hass.data[DOMAIN]["runtime"]


def music_assistant_status(hass: HomeAssistant) -> dict[str, Any]:
    """Return the redacted MA connection status the card reads from ``get_context``."""
    return engine_runtime(hass).context()["music_assistant"]


def engine_entity_id(
    hass: HomeAssistant, entry: MockConfigEntry, domain: str, key: str
) -> str | None:
    """Return the entity ID of the Engine entity whose unique ID is ``<entry_id>_<key>``."""
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}_{key}")


async def async_press_button(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> None:
    """Press an Engine button, for example ``refresh_state`` to recompute every sensor."""
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": engine_entity_id(hass, entry, "button", key)},
        blocking=True,
    )
    await hass.async_block_till_done()


async def async_engine_updated(hass: HomeAssistant) -> None:
    """Send the Engine's update signal, as every storage write does, and settle."""
    async_dispatcher_send(hass, SIGNAL_ENGINE_UPDATED)
    await hass.async_block_till_done()


async def async_setup_engine(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set up an Engine entry and wait until it is connected to Music Assistant.

    The startup probe can run before the Music Assistant WebSocket has authenticated,
    so this then runs the same probe as the card's ``bootstrap/get`` to give every
    connection sensor a deterministic result.
    """
    if entry.entry_id not in {item.entry_id for item in hass.config_entries.async_entries(DOMAIN)}:
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    await async_wait_for(
        lambda: music_assistant_status(hass)["authenticated"],
        message="the Music Assistant WebSocket to authenticate",
    )
    await engine_runtime(hass).async_bootstrap_snapshot()
    async_dispatcher_send(hass, SIGNAL_ENGINE_UPDATED)
    await hass.async_block_till_done()


@pytest.fixture
async def loaded_entry(
    hass: HomeAssistant,
    fake_ma: FakeMusicAssistant,
    mock_music_assistant: MusicAssistantStub,
    config_entry: MockConfigEntry,
) -> AsyncGenerator[MockConfigEntry]:
    """Return an Engine config entry that is loaded and connected to ``fake_ma``."""
    await async_setup_engine(hass, config_entry)
    yield config_entry
    if config_entry.state is ConfigEntryState.LOADED:
        assert await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()
