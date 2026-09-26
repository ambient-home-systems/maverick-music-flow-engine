"""HOMEii Flow Engine integration."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from aiohttp import web

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
    UnknownUser,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.service import async_register_admin_service

from .artwork_proxy import (
    ARTWORK_SECURITY_HEADERS,
    ArtworkFetcher,
    ArtworkPayload,
    artwork_fetch_urls,
    async_get_strict_artwork_session,
    home_assistant_base_url,
)
from .authorization import (
    ACCESS_MANAGE,
    access_denial,
    http_access_level,
    payload_targets,
    requires_target,
    service_access_level,
    stored_targets,
)
from .command_bridge import (
    HTTP_COMMAND_SCHEMAS,
    music_assistant_command_allowed,
    strip_internal_keys,
)
from .media_url_policy import MediaUrlNotAllowed
from .sendspin_bridge import HomeiiFlowSendspinView

from .const import (
    CONF_ALLOW_LOCAL_MEDIA_URLS,
    CONF_ALLOW_NON_ADMIN_MANAGEMENT,
    CONF_ENABLE_EXPERIMENTAL,
    CONF_INSTANCE_ID,
    CONF_MUSIC_ASSISTANT_EXTERNAL_URL,
    CONF_MUSIC_ASSISTANT_TOKEN,
    CONF_MUSIC_ASSISTANT_URL,
    CONF_PROFILE_ID,
    CONFIG_ENTRY_VERSION,
    DEFAULT_INSTANCE_ID,
    DEFAULT_PROFILE_ID,
    DOMAIN,
    NOT_LOADED_MESSAGE,
    PLATFORMS,
)
from .queue_settings import FIELD_TYPES
from .runtime import HomeiiFlowRuntime
from .websocket_api import async_register_websocket_commands
from .interface_preferences import save_preferences

_LOGGER = logging.getLogger(__name__)
FRONTEND_DIR = Path(__file__).parent / "frontend"

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_SET_VOLUME_RULE = "set_volume_rule"
SERVICE_DELETE_VOLUME_RULE = "delete_volume_rule"
SERVICE_CLEAR_VOLUME_RULES = "clear_volume_rules"
SERVICE_SET_SCHEDULE = "set_schedule"
SERVICE_DELETE_SCHEDULE = "delete_schedule"
SERVICE_RUN_SCHEDULE = "run_schedule"
SERVICE_SET_TIMER = "set_timer"
SERVICE_DELETE_TIMER = "delete_timer"
SERVICE_ANNOUNCE = "announce"
SERVICE_PLAY_MEDIA = "play_media"
SERVICE_PLAYER_COMMAND = "player_command"
SERVICE_SET_QUEUE_SETTINGS = "set_queue_settings"
SERVICE_SET_QUEUE_SETTINGS_SCHEMA = vol.Schema({vol.Optional(key): kind for key, kind in FIELD_TYPES.items()})
SERVICE_TRANSFER_QUEUE = "transfer_queue"
SERVICE_RUN_ORCHESTRATION = "run_orchestration"
SERVICE_SET_SCREENSAVER = "set_screensaver"
SERVICE_SHOW_SCREENSAVER = "show_screensaver"

SERVICE_SET_VOLUME_RULE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Required("player"): str,
        vol.Required("max_volume"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        vol.Optional("start_time", default=""): str,
        vol.Optional("end_time", default=""): str,
        vol.Optional("days", default=list): [int],
        vol.Optional("enabled", default=True): bool,
    }
)

SERVICE_CLEAR_VOLUME_RULES_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
    }
)

SERVICE_DELETE_VOLUME_RULE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Required("player"): str,
    }
)

SERVICE_SET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Optional("id"): str,
        vol.Optional("schedule_id"): str,
        vol.Optional("name", default="HOMEii schedule"): str,
        vol.Optional("kind", default="wake_playback"): str,
        vol.Optional("action", default="wake_playback"): str,
        vol.Required("player"): str,
        vol.Optional("media_id", default=""): str,
        vol.Optional("media_content_id", default=""): str,
        vol.Optional("playlist", default=""): str,
        vol.Optional("media_type", default="music"): str,
        vol.Optional("media_content_type", default=""): str,
        vol.Optional("media_name"): str,
        vol.Optional("playlist_name"): str,
        vol.Optional("media_mode"): str,
        vol.Optional("selection_mode"): str,
        vol.Optional("enqueue", default="play"): str,
        vol.Optional("radio_mode", default=False): bool,
        vol.Optional("retry_attempts", default=4): vol.All(vol.Coerce(int), vol.Range(min=1, max=12)),
        vol.Optional("retry_delay", default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=30)),
        vol.Required("time"): str,
        vol.Optional("days", default=list): [int],
        vol.Optional("volume"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        vol.Optional("enabled", default=True): bool,
        vol.Optional("after_run", default="keep"): str,
    }
)

SERVICE_DELETE_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Required("id"): str,
        vol.Optional("schedule_id"): str,
    }
)

SERVICE_RUN_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Required("id"): str,
        vol.Optional("schedule_id"): str,
    }
)

SERVICE_SET_TIMER_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Optional("id"): str,
        vol.Optional("timer_id"): str,
        vol.Required("player"): str,
        vol.Optional("action", default="stop"): str,
        vol.Optional("minutes"): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
        vol.Optional("ends_at"): str,
        vol.Optional("origin"): str,
        vol.Optional("enabled", default=True): bool,
    }
)

SERVICE_DELETE_TIMER_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Optional("id"): str,
        vol.Optional("timer_id"): str,
        vol.Optional("player"): str,
    }
)

SERVICE_ANNOUNCE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Required("message"): str,
        vol.Optional("player"): str,
        vol.Optional("players", default=list): [str],
        vol.Optional("entity_id"): str,
        vol.Optional("volume"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        vol.Optional("language"): str,
        vol.Optional("tts_entity"): str,
        vol.Optional("announcement_tts_entity"): str,
        vol.Optional("target"): str,
    }
)

SERVICE_PLAY_MEDIA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Required("player"): str,
        vol.Required("media_id"): str,
        vol.Optional("media_type", default="music"): str,
        vol.Optional("enqueue", default="play"): str,
        vol.Optional("radio_mode", default=False): bool,
    }
)

SERVICE_PLAYER_COMMAND_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Required("player"): str,
        vol.Required("command"): str,
        vol.Optional("volume"): vol.Any(int, float),
        vol.Optional("volume_level"): vol.Any(int, float),
        vol.Optional("shuffle", default=True): bool,
        vol.Optional("is_volume_muted"): bool,
        vol.Optional("autoplay_enabled"): bool,
        vol.Optional("crossfade_enabled"): bool,
        vol.Optional("seek_position"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional("speed"): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=3.0)),
    }
)

SERVICE_TRANSFER_QUEUE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Required("source_player"): str,
        vol.Required("target_player"): str,
        vol.Optional("auto_play", default=True): bool,
    }
)

SERVICE_SET_SCREENSAVER_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Optional("enabled"): bool,
        vol.Optional("timeout_seconds"): vol.All(vol.Coerce(int), vol.Range(min=15, max=3600)),
        vol.Optional("mode"): vol.In(["auto", "clock", "lyrics"]),
        vol.Optional("auto_lyrics_when_playing"): bool,
        vol.Optional("clock_mode"): vol.In(["digital", "analog"]),
        vol.Optional("message"): str,
        vol.Optional("show_artwork"): bool,
        vol.Optional("music_assistant_url"): str,
        vol.Optional("ma_url"): str,
        vol.Optional("music_assistant_external_url"): str,
    }
)

SERVICE_SHOW_SCREENSAVER_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PROFILE_ID, default=DEFAULT_PROFILE_ID): str,
        vol.Optional("source", default="service"): str,
    }
)


def _looks_like_artwork_url(value: str) -> bool:
    """Return whether a string looks like an artwork URL/path."""
    clean = value.strip()
    if not clean:
        return False
    lower = clean.lower()
    return (
        lower.startswith(("http://", "https://", "/", "data:image/"))
        or "imageproxy" in lower
        or "media_player_proxy" in lower
        or lower.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))
    )


def _append_artwork_candidate(candidates: list[str], value: Any) -> None:
    """Append a unique artwork candidate."""
    if isinstance(value, dict):
        for key in ("url", "path", "src", "uri", "image", "thumbnail", "thumb"):
            _append_artwork_candidate(candidates, value.get(key))
        return
    if isinstance(value, list):
        for item in value:
            _append_artwork_candidate(candidates, item)
        return
    clean = str(value or "").strip()
    ignored = {"builtin", "jpeg", "jpg", "png", "webp", "gif", "image", "images", "default"}
    lower = clean.lower()
    if clean and clean not in candidates and (_looks_like_artwork_url(clean) or (len(clean) > 5 and lower not in ignored)):
        candidates.append(clean)


def _collect_artwork_candidates(value: Any, candidates: list[str], *, depth: int = 0, art_context: bool = False) -> None:
    """Collect artwork candidates from common Music Assistant response shapes."""
    if value is None or depth > 8:
        return
    if isinstance(value, str):
        if art_context:
            _append_artwork_candidate(candidates, value)
        return
    if isinstance(value, list):
        for item in value[:40]:
            _collect_artwork_candidates(item, candidates, depth=depth + 1, art_context=art_context)
        return
    if not isinstance(value, dict):
        return
    for key, child in value.items():
        clean_key = str(key or "").lower()
        is_art_key = any(
            token in clean_key
            for token in ("art", "cover", "image", "thumbnail", "thumb", "picture", "fanart")
        )
        if is_art_key:
            _append_artwork_candidate(candidates, child)
        _collect_artwork_candidates(child, candidates, depth=depth + 1, art_context=art_context or is_art_key)


class HomeiiFlowArtworkProxyView(HomeAssistantView):
    """Proxy current player artwork through Home Assistant for dashboard agents.

    Every upstream fetch goes through the hardened helper in ``artwork_proxy``: only
    validated raster images are served, private addresses are refused for anything but
    the configured Music Assistant and Home Assistant URLs, and fetch URLs are never built
    from the incoming request.
    """

    url = "/api/maverick_music_flow/artwork/{entity_id}"
    name = "api:maverick_music_flow:artwork"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the artwork proxy."""
        self.hass = hass

    def _artwork_fetcher(self, runtime: HomeiiFlowRuntime) -> tuple[ArtworkFetcher, list[str], str]:
        """Return the hardened fetcher plus the base URLs fetch URLs may be built from."""
        ma_base_urls = runtime.music_assistant_base_urls()
        ha_base_url = home_assistant_base_url(self.hass)
        fetcher = ArtworkFetcher(
            trusted_session=async_get_clientsession(self.hass),
            strict_session=async_get_strict_artwork_session(self.hass),
            ma_base_urls=ma_base_urls,
            ma_tokens=runtime.music_assistant_tokens(),
            ha_base_url=ha_base_url,
        )
        return fetcher, ma_base_urls, ha_base_url

    @staticmethod
    async def _async_fetch_source(
        fetcher: ArtworkFetcher, source: str, ma_base_urls: list[str], ha_base_url: str
    ) -> ArtworkPayload | None:
        """Return the first validated image among the candidate URLs for a source."""
        for url in artwork_fetch_urls(source, ma_base_urls, ha_base_url):
            payload = await fetcher.async_fetch(url)
            if payload is not None:
                return payload
        return None

    async def get(self, request: web.Request, entity_id: str) -> web.Response:
        """Return current artwork for a media player entity."""
        runtime = async_get_runtime(self.hass)
        if not runtime.active:
            raise web.HTTPServiceUnavailable(text=NOT_LOADED_MESSAGE)
        player = next(
            (
                item
                for item in runtime.media_players_snapshot()
                if str(item.get("entity_id") or "").strip() == entity_id
            ),
            None,
        )
        if not player:
            raise web.HTTPNotFound(text="player artwork source not found")

        candidates = [
            *[
                str(item or "").strip()
                for item in player.get("artwork_candidates", [])
                if str(item or "").strip()
            ],
            str(player.get("entity_picture") or "").strip(),
            str(player.get("media_image_url") or "").strip(),
        ]
        try:
            queue_response = await runtime.async_get_queue(
                {
                    "entity_id": entity_id,
                    "queue_id": str(player.get("active_queue") or "").strip(),
                    "include_diagnostics": True,
                }
            )
            _collect_artwork_candidates(queue_response.get("data"), candidates)
        except Exception:  # noqa: BLE001 - artwork proxy must remain best effort
            pass
        sources: list[str] = []
        for candidate in candidates:
            source = candidate
            if candidate.startswith("/api/maverick_music_flow/artwork/item/"):
                # Resolve the Engine's own opaque URLs locally instead of fetching
                # them back through Home Assistant.
                source = runtime.resolve_artwork_source(candidate.rsplit("/", 1)[-1].split("?", 1)[0])
            if source and source not in sources:
                sources.append(source)
        fetcher, ma_base_urls, ha_base_url = self._artwork_fetcher(runtime)
        for source in sources:
            payload = await self._async_fetch_source(fetcher, source, ma_base_urls, ha_base_url)
            if payload is None:
                continue
            return web.Response(
                body=payload.body,
                content_type=payload.content_type,
                headers={
                    "Cache-Control": "no-store, max-age=0",
                    "X-HOMEii-Flow-Artwork-Source": "proxy",
                    **ARTWORK_SECURITY_HEADERS,
                },
            )
        raise web.HTTPNotFound(text="artwork could not be loaded")


class HomeiiFlowItemArtworkProxyView(HomeiiFlowArtworkProxyView):
    """Proxy registered queue/library artwork through Home Assistant.

    This route needs no login so plain ``<img>`` tags work; the opaque token is the only
    credential, which is why tokens are keyed with a per-installation secret.
    """

    url = "/api/maverick_music_flow/artwork/item/{token}"
    name = "api:maverick_music_flow:item_artwork"
    requires_auth = False

    @staticmethod
    def _artwork_response(
        request: web.Request,
        body: bytes,
        content_type: str,
        source_label: str,
    ) -> web.Response:
        """Return cache-friendly artwork with conditional request support."""
        etag = f'"{hashlib.sha256(body).hexdigest()}"'
        headers = {
            "Cache-Control": "private, max-age=1800, stale-while-revalidate=86400",
            "ETag": etag,
            "X-HOMEii-Flow-Artwork-Source": source_label,
            **ARTWORK_SECURITY_HEADERS,
        }
        if request.headers.get("If-None-Match", "").strip() == etag:
            return web.Response(status=304, headers=headers)
        return web.Response(body=body, content_type=content_type, headers=headers)

    async def get(self, request: web.Request, token: str) -> web.Response:
        """Return artwork for an opaque Engine token."""
        runtime = async_get_runtime(self.hass)
        # This route needs no login, so an unloaded Engine answers like an unknown token.
        if not runtime.active:
            raise web.HTTPNotFound(text="artwork token not found or expired")
        source = runtime.resolve_artwork_source(token)
        if not source:
            raise web.HTTPNotFound(text="artwork token not found or expired")
        cached = runtime.cached_artwork_content(source)
        if cached:
            body, content_type = cached
            return self._artwork_response(request, body, content_type, "memory-cache")
        fetcher, ma_base_urls, ha_base_url = self._artwork_fetcher(runtime)
        payload = await self._async_fetch_source(fetcher, source, ma_base_urls, ha_base_url)
        if payload is None:
            raise web.HTTPNotFound(text="artwork could not be loaded")
        runtime.cache_artwork_content(source, payload.body, payload.content_type)
        return self._artwork_response(request, payload.body, payload.content_type, "item-proxy")


class HomeiiFlowCommandView(HomeAssistantView):
    """Expose a fixed set of Engine commands over authenticated HTTP for frontend fallbacks.

    The card falls back to this view for reads (get_context, bootstrap/get, queue/get,
    library/get, favorites/get, search/get). It also accepts two writes, favorites/set and
    ma/command. Every command validates its body with the same schema as the matching
    WebSocket command and applies the same authorization (see ``authorization.py``), so
    HTTP grants nothing that WebSocket does not. ma/command is limited to the Music
    Assistant command allowlist, and a refused request returns HTTP 403.
    """

    url = "/api/maverick_music_flow/command/{command:.+}"
    name = "api:maverick_music_flow:command"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the command view."""
        self.hass = hass

    async def post(self, request: web.Request, command: str) -> web.Response:
        """Run one allowed Engine command for clients that cannot use WebSocket."""
        runtime = async_get_runtime(self.hass)
        if not runtime.active:
            raise web.HTTPServiceUnavailable(text=NOT_LOADED_MESSAGE)
        clean_command = str(command or "").strip().strip("/")
        schema = HTTP_COMMAND_SCHEMAS.get(clean_command)
        if schema is None:
            raise web.HTTPNotFound(text="unsupported HOMEii Flow Engine command")
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - malformed JSON should become a clear HTTP error
            body = {}
        if not isinstance(body, dict):
            body = {}
        try:
            payload = schema(strip_internal_keys(body))
        except vol.Invalid as err:
            raise web.HTTPBadRequest(text=f"invalid request: {err}") from err
        # "type" is the WebSocket command type the card also sends over HTTP.
        payload.pop("type", None)
        if clean_command == "ma/command" and not music_assistant_command_allowed(
            str(payload["command"]).strip()
        ):
            raise web.HTTPForbidden(text="Music Assistant command is not allowed")
        user = request["hass_user"]
        level = http_access_level(clean_command, payload)
        denial = access_denial(
            level,
            is_admin=bool(user.is_admin),
            can_control=lambda entity_id: bool(user.permissions.check_entity(entity_id, POLICY_CONTROL)),
            targets=[
                runtime.control_entity_id(target)
                for target in payload_targets(clean_command, payload)
            ],
            require_target=requires_target(clean_command, level),
        )
        if denial is not None:
            raise web.HTTPForbidden(text=denial.reason)
        instance_id = str(payload.get(CONF_INSTANCE_ID) or "").strip() or None
        profile_id = str(payload.get(CONF_PROFILE_ID) or "").strip() or None
        if clean_command == "get_context":
            result = runtime.context(instance_id=instance_id, profile_id=profile_id)
        elif clean_command == "bootstrap/get":
            result = runtime.bootstrap_snapshot(instance_id=instance_id, profile_id=profile_id)
        elif clean_command == "queue/get":
            result = await runtime.async_get_queue(payload)
        elif clean_command == "library/get":
            result = await runtime.async_get_library(payload)
        elif clean_command == "favorites/get":
            result = await runtime.async_get_favorites(payload)
        elif clean_command == "favorites/set":
            result = await runtime.async_set_favorite(payload)
        elif clean_command == "search/get":
            result = await runtime.async_get_search(payload)
        else:  # ma/command
            result = await runtime.async_music_assistant_command(payload)
        return web.json_response(result)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the Engine actions; everything else starts with a config entry.

    Actions are registered here so automations using them validate even while the entry
    is not loaded; they then raise a "not loaded" error.
    """
    _async_register_services(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate an older config entry to the current version."""
    if entry.version > CONFIG_ENTRY_VERSION:
        # Written by a newer Engine release; refuse rather than guess.
        return False
    if entry.version == 1:
        # Version 1 options flows copied the MA token into options, where it overrode
        # the one in data. Keep that effective token, in data only.
        data = dict(entry.data)
        options = dict(entry.options)
        options_token = str(options.pop(CONF_MUSIC_ASSISTANT_TOKEN, None) or "").strip()
        if options_token:
            data[CONF_MUSIC_ASSISTANT_TOKEN] = options_token
        hass.config_entries.async_update_entry(
            entry, data=data, options=options, version=CONFIG_ENTRY_VERSION
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up HOMEii Flow Engine from a config entry."""
    try:
        runtime = await async_start_runtime(hass)
        _register_entry(runtime, entry)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # Home Assistant does not call async_unload_entry after a failed setup.
        await _async_release_runtime(hass, entry.entry_id)
        raise
    entry.async_on_unload(entry.add_update_listener(_async_update_entry))
    return True


def _register_entry(runtime: HomeiiFlowRuntime, entry: ConfigEntry) -> None:
    """Pass a config entry's settings to the runtime."""
    instance_id = str(entry.data.get(CONF_INSTANCE_ID) or DEFAULT_INSTANCE_ID)
    profile_id = str(entry.options.get(CONF_PROFILE_ID) or entry.data.get(CONF_PROFILE_ID) or DEFAULT_PROFILE_ID)
    enable_experimental = bool(entry.options.get(CONF_ENABLE_EXPERIMENTAL, False))
    allow_non_admin_management = bool(entry.options.get(CONF_ALLOW_NON_ADMIN_MANAGEMENT, False))
    allow_local_media_urls = bool(entry.options.get(CONF_ALLOW_LOCAL_MEDIA_URLS, False))
    music_assistant_url = str(
        entry.options.get(CONF_MUSIC_ASSISTANT_URL)
        or entry.data.get(CONF_MUSIC_ASSISTANT_URL)
        or ""
    ).strip()
    music_assistant_external_url = str(
        entry.options.get(CONF_MUSIC_ASSISTANT_EXTERNAL_URL)
        or entry.data.get(CONF_MUSIC_ASSISTANT_EXTERNAL_URL)
        or ""
    ).strip()
    music_assistant_token = str(entry.data.get(CONF_MUSIC_ASSISTANT_TOKEN) or "").strip()
    runtime.register_entry(
        entry.entry_id,
        instance_id=instance_id,
        profile_id=profile_id,
        title=entry.title,
        enable_experimental=enable_experimental,
        music_assistant_url=music_assistant_url,
        music_assistant_external_url=music_assistant_external_url,
        music_assistant_token=music_assistant_token,
        allow_non_admin_management=allow_non_admin_management,
        allow_local_media_urls=allow_local_media_urls,
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a HOMEii Flow Engine config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False
    await _async_release_runtime(hass, entry.entry_id)
    return True


async def _async_release_runtime(hass: HomeAssistant, entry_id: str) -> None:
    """Forget a config entry and stop all Engine work when it was the last one."""
    runtime = async_get_runtime(hass)
    runtime.unregister_entry(entry_id)
    if not runtime.entries:
        await runtime.async_shutdown()


async def _async_update_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options updates."""
    await hass.config_entries.async_reload(entry.entry_id)


def async_get_runtime(hass: HomeAssistant) -> HomeiiFlowRuntime:
    """Return the HOMEii Flow runtime."""
    data = hass.data.setdefault(DOMAIN, {})
    runtime = data.get("runtime")
    if isinstance(runtime, HomeiiFlowRuntime):
        return runtime
    runtime = HomeiiFlowRuntime(hass)
    data["runtime"] = runtime
    return runtime


async def async_start_runtime(hass: HomeAssistant) -> HomeiiFlowRuntime:
    """Start the shared runtime and register its WebSocket commands and HTTP views once.

    Home Assistant cannot unregister views, static paths or WebSocket commands, so they
    stay registered after the last entry unloads and refuse requests while the runtime
    is not active.
    """
    data = hass.data.setdefault(DOMAIN, {})
    runtime = async_get_runtime(hass)
    await runtime.async_start()
    if not data.get("websocket_registered"):
        async_register_websocket_commands(hass)
        data["websocket_registered"] = True
    if not data.get("frontend_registered"):
        await hass.http.async_register_static_paths(
            [StaticPathConfig("/maverick_music_flow", str(FRONTEND_DIR), cache_headers=False)]
        )
        data["frontend_registered"] = True
    if not data.get("artwork_proxy_registered"):
        hass.http.register_view(HomeiiFlowArtworkProxyView(hass))
        hass.http.register_view(HomeiiFlowItemArtworkProxyView(hass))
        hass.http.register_view(HomeiiFlowCommandView(hass))
        hass.http.register_view(HomeiiFlowSendspinView(hass))
        data["artwork_proxy_registered"] = True
    _LOGGER.debug("HOMEii Flow Engine runtime started")
    return runtime


def _async_require_loaded(hass: HomeAssistant) -> None:
    """Raise ServiceValidationError while no config entry is loaded."""
    runtime = hass.data.get(DOMAIN, {}).get("runtime")
    if runtime is None or not runtime.active:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_loaded")


async def _async_check_service_access(hass: HomeAssistant, service: str, call: ServiceCall) -> None:
    """Raise Unauthorized when the person behind a service call may not run it.

    Calls without a user context (automations, scripts, the system user) always pass.
    Administrators may call every service; other users follow the same rules as the
    WebSocket commands: playback services need the ``control`` entity permission on the
    target players, schedule/timer/volume-rule services additionally need the
    "Allow non-admin users to manage ..." option, and configuration services are refused.
    """
    user_id = call.context.user_id
    if not user_id:
        return
    user = await hass.auth.async_get_user(user_id)
    if user is None:
        raise UnknownUser(context=call.context)
    runtime = async_get_runtime(hass)
    payload = dict(call.data)
    level = service_access_level(service)
    targets = payload_targets(service, payload)
    if level == ACCESS_MANAGE:
        profile_id = str(payload.get(CONF_PROFILE_ID) or DEFAULT_PROFILE_ID)
        targets += stored_targets(
            service,
            payload,
            schedules=runtime.schedules(profile_id),
            timers=runtime.timers(profile_id),
            volume_rules=runtime.volume_rules(profile_id),
            default_profile_id=DEFAULT_PROFILE_ID,
        )
    denial = access_denial(
        level,
        is_admin=bool(user.is_admin),
        can_control=lambda entity_id: bool(user.permissions.check_entity(entity_id, POLICY_CONTROL)),
        targets=[runtime.control_entity_id(target) for target in targets],
        require_target=requires_target(service, level),
        management_allowed=runtime.non_admin_management_allowed(),
    )
    if denial is None:
        return
    if denial.entity_id:
        raise Unauthorized(context=call.context, entity_id=denial.entity_id, permission=POLICY_CONTROL)
    raise Unauthorized(context=call.context)


def _async_register_guarded_service(
    hass: HomeAssistant,
    service: str,
    handler: Any,
    schema: vol.Schema | None = None,
) -> None:
    """Register a service whose handler runs only after the authorization check."""
    if hass.services.has_service(DOMAIN, service):
        return

    async def guarded(call: ServiceCall) -> None:
        _async_require_loaded(hass)
        await _async_check_service_access(hass, service, call)
        await handler(call)

    hass.services.async_register(DOMAIN, service, guarded, schema=schema)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register optional automation-facing services."""
    async def set_interface_preferences(call: ServiceCall) -> None:
        await save_preferences(hass.data[DOMAIN]["runtime"], dict(call.data))

    _async_register_guarded_service(hass, "set_interface_preferences", set_interface_preferences,
        schema=vol.Schema({vol.Optional("profile_id"): str, vol.Optional("night_mode"): str,
            vol.Optional("night_start"): str, vol.Optional("night_end"): str, vol.Optional("night_days"): [int]}))


    async def set_volume_rule(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_set_volume_rule(dict(call.data))

    async def clear_volume_rules(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_clear_volume_rules(str(call.data.get(CONF_PROFILE_ID) or DEFAULT_PROFILE_ID))

    async def delete_volume_rule(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_delete_volume_rule(dict(call.data))

    async def set_schedule(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_set_schedule(dict(call.data))

    async def delete_schedule(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_delete_schedule(dict(call.data))

    async def run_schedule(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_run_schedule_now(dict(call.data))

    async def set_timer(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_set_timer(dict(call.data))

    async def delete_timer(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_delete_timer(dict(call.data))

    async def announce(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        try:
            await runtime.async_send_announcement(dict(call.data))
        except MediaUrlNotAllowed as error:
            raise ServiceValidationError(str(error)) from error

    async def play_media(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        try:
            await runtime.async_play_media(dict(call.data))
        except MediaUrlNotAllowed as error:
            raise ServiceValidationError(str(error)) from error

    async def set_queue_settings(call: ServiceCall) -> None:
        _async_require_loaded(hass)
        await async_get_runtime(hass).async_queue_settings({"values": dict(call.data)})

    async def player_command(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        try:
            await runtime.async_player_command(dict(call.data))
        except ValueError as error:
            raise ServiceValidationError(str(error)) from error
        except RuntimeError as error:
            raise HomeAssistantError(str(error)) from error

    async def transfer_queue(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_transfer_queue(dict(call.data))

    async def run_orchestration(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_tick_orchestration()

    async def set_screensaver(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_set_screensaver_config(dict(call.data))

    async def show_screensaver(call: ServiceCall) -> None:
        runtime = async_get_runtime(hass)
        await runtime.async_request_screensaver_show(dict(call.data))

    _async_register_guarded_service(hass, SERVICE_SET_VOLUME_RULE, set_volume_rule, schema=SERVICE_SET_VOLUME_RULE_SCHEMA)
    _async_register_guarded_service(hass, SERVICE_CLEAR_VOLUME_RULES, clear_volume_rules, schema=SERVICE_CLEAR_VOLUME_RULES_SCHEMA)
    _async_register_guarded_service(hass, SERVICE_DELETE_VOLUME_RULE, delete_volume_rule, schema=SERVICE_DELETE_VOLUME_RULE_SCHEMA)
    _async_register_guarded_service(hass, SERVICE_SET_SCHEDULE, set_schedule, schema=SERVICE_SET_SCHEDULE_SCHEMA)
    _async_register_guarded_service(hass, SERVICE_DELETE_SCHEDULE, delete_schedule, schema=SERVICE_DELETE_SCHEDULE_SCHEMA)
    _async_register_guarded_service(hass, SERVICE_RUN_SCHEDULE, run_schedule, schema=SERVICE_RUN_SCHEDULE_SCHEMA)
    _async_register_guarded_service(hass, SERVICE_SET_TIMER, set_timer, schema=SERVICE_SET_TIMER_SCHEMA)
    _async_register_guarded_service(hass, SERVICE_DELETE_TIMER, delete_timer, schema=SERVICE_DELETE_TIMER_SCHEMA)
    _async_register_guarded_service(hass, SERVICE_ANNOUNCE, announce, schema=SERVICE_ANNOUNCE_SCHEMA)
    _async_register_guarded_service(hass, SERVICE_PLAY_MEDIA, play_media, schema=SERVICE_PLAY_MEDIA_SCHEMA)
    if not hass.services.has_service(DOMAIN, SERVICE_SET_QUEUE_SETTINGS):
        async_register_admin_service(hass, DOMAIN, SERVICE_SET_QUEUE_SETTINGS, set_queue_settings, schema=SERVICE_SET_QUEUE_SETTINGS_SCHEMA)
    _async_register_guarded_service(hass, SERVICE_PLAYER_COMMAND, player_command, schema=SERVICE_PLAYER_COMMAND_SCHEMA)
    _async_register_guarded_service(hass, SERVICE_TRANSFER_QUEUE, transfer_queue, schema=SERVICE_TRANSFER_QUEUE_SCHEMA)
    _async_register_guarded_service(hass, SERVICE_RUN_ORCHESTRATION, run_orchestration)
    _async_register_guarded_service(hass, SERVICE_SET_SCREENSAVER, set_screensaver, schema=SERVICE_SET_SCREENSAVER_SCHEMA)
    _async_register_guarded_service(hass, SERVICE_SHOW_SCREENSAVER, show_screensaver, schema=SERVICE_SHOW_SCREENSAVER_SCHEMA)
