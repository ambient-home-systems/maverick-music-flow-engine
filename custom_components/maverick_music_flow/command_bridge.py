"""Music Assistant command bridge policy and schemas for external Engine payloads.

This module has no Home Assistant imports so the policy can be tested directly. The
WebSocket commands and the HTTP command view share these schemas, so both transports
accept exactly the same fields.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from .const import CONF_INSTANCE_ID, CONF_PROFILE_ID

_LOGGER = logging.getLogger(__name__)

# Keys with this prefix are Engine-internal flags. They are never accepted from callers.
INTERNAL_PAYLOAD_PREFIX = "_homeii_"

# Library roots the Engine (async_get_library) and the card (library and discovery views)
# read through music/<root>/library_items.
LIBRARY_ITEM_ROOTS = (
    "albums",
    "artists",
    "audiobooks",
    "genres",
    "playlists",
    "podcasts",
    "radios",
    "tracks",
)

# Exact Music Assistant commands that may run with the Engine's MA token. Each entry is
# sent by the Engine itself or by the HOMEii Music Flow card. Anything else is denied,
# including player and group creation or removal, configuration, providers, sync,
# import/export and authentication.
MUSIC_ASSISTANT_COMMAND_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Players
        "players/all",
        "players/cmd/play",
        "players/cmd/pause",
        "players/cmd/stop",
        "players/cmd/next",
        "players/cmd/previous",
        "players/cmd/volume_set",
        "players/cmd/volume_mute",
        "players/cmd/group_many",
        "players/cmd/set_members",
        "players/cmd/ungroup",
        # Queues
        "player_queues/all",
        "player_queues/get",
        "player_queues/get_active_queue",
        "player_queues/items",
        "player_queues/play_media",
        "player_queues/play_index",
        "player_queues/shuffle",
        "player_queues/repeat",
        "player_queues/crossfade",
        "player_queues/autoplay",
        "player_queues/seek",
        "player_queues/set_playback_speed",
        "player_queues/move_item",
        "player_queues/delete_item",
        "player_queues/clear",
        "player_queues/transfer",
        # Media reads
        "music/search",
        "music/browse",
        "music/item_by_uri",
        "music/recommendations",
        "music/recommendations/items",
        "music/recently_played_items",
        "music/in_progress_items",
        "music/tracks/similar_tracks",
        "music/albums/album_tracks",
        "music/artists/artist_albums",
        "music/artists/artist_tracks",
        "music/playlists/playlist_tracks",
        "music/podcasts/podcast_episodes",
        *(f"music/{root}/library_items" for root in LIBRARY_ITEM_ROOTS),
        "metadata/get_track_lyrics",
        "audio_analysis/wave_form",
        # Library writes the card offers (favorites, add to library, add to playlist)
        "music/favorites/add_item",
        "music/favorites/remove_item",
        "music/library/add_item",
        "music/playlists/add_playlist_tracks",
        # AI radio DJ
        "ai_radio/hosts/list",
        "ai_radio/queue_dj/status",
        "ai_radio/queue_dj/set",
    }
)


def music_assistant_command_allowed(command: str) -> bool:
    """Return whether a Music Assistant command may run through the card bridge."""
    if command in MUSIC_ASSISTANT_COMMAND_ALLOWLIST:
        return True
    _LOGGER.debug("Denied Music Assistant command through the card bridge: %.120r", command)
    return False


def strip_internal_keys(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an external payload without Engine-internal flags."""
    return {
        key: value
        for key, value in payload.items()
        if not (isinstance(key, str) and key.startswith(INTERNAL_PAYLOAD_PREFIX))
    }


BASE_SCHEMA = {
    vol.Optional("card_id"): str,
    vol.Optional("card_version"): str,
    vol.Optional(CONF_INSTANCE_ID): str,
    vol.Optional(CONF_PROFILE_ID): str,
    vol.Optional("selected_player"): str,
    vol.Optional("source"): str,
}

MA_COMMAND_FIELDS = {
    vol.Required("command"): str,
    vol.Optional("args", default=dict): dict,
}

QUEUE_GET_FIELDS = {
    vol.Optional("entity_id"): str,
    vol.Optional("selected_player"): str,
    vol.Optional("queue_id"): str,
    vol.Optional("limit_before"): vol.Any(str, int),
    vol.Optional("limit_after"): vol.Any(str, int),
}

LIBRARY_GET_FIELDS = {
    vol.Optional("media_type"): str,
    vol.Optional("type"): str,
    vol.Optional("query"): str,
    vol.Optional("search"): str,
    vol.Optional("search_query"): str,
    vol.Optional("name"): str,
    vol.Optional("order_by"): str,
    vol.Optional("limit"): int,
    vol.Optional("offset", default=0): vol.All(int, vol.Range(min=0)),
    vol.Optional("favorite", default=False): bool,
    vol.Optional("favorites_only", default=False): bool,
    vol.Optional("compact", default=False): bool,
}

FAVORITES_GET_FIELDS = {
    vol.Optional("media_types"): [str],
    vol.Optional("limit"): int,
    vol.Optional("refresh", default=False): bool,
}

FAVORITES_SET_FIELDS = {
    vol.Required("favorite"): bool,
    vol.Optional("uri"): str,
    vol.Optional("media_type"): str,
    vol.Optional("item_id"): str,
    vol.Optional("provider"): str,
    vol.Optional("library_item_id"): str,
    vol.Optional("entry"): dict,
    vol.Optional("remove_args"): dict,
}

SEARCH_GET_FIELDS = {
    vol.Optional("query"): str,
    vol.Optional("search"): str,
    vol.Optional("search_query"): str,
    vol.Optional("name"): str,
    vol.Optional("media_type"): vol.Any(str, [str]),
    vol.Optional("media_types"): [str],
    vol.Optional("limit"): int,
    vol.Optional("library_only", default=False): bool,
    vol.Optional("provider_only", default=False): bool,
}

# The card posts the same message over HTTP as over WebSocket, including "type". The
# view drops "type" after validation, as the WebSocket handlers do.
_HTTP_BASE_SCHEMA = {vol.Optional("type"): str, **BASE_SCHEMA}

HTTP_COMMAND_SCHEMAS: dict[str, vol.Schema] = {
    "get_context": vol.Schema(_HTTP_BASE_SCHEMA),
    "bootstrap/get": vol.Schema(_HTTP_BASE_SCHEMA),
    "queue/get": vol.Schema({**_HTTP_BASE_SCHEMA, **QUEUE_GET_FIELDS}),
    "library/get": vol.Schema({**_HTTP_BASE_SCHEMA, **LIBRARY_GET_FIELDS}),
    "favorites/get": vol.Schema({**_HTTP_BASE_SCHEMA, **FAVORITES_GET_FIELDS}),
    "favorites/set": vol.Schema({**_HTTP_BASE_SCHEMA, **FAVORITES_SET_FIELDS}),
    "search/get": vol.Schema({**_HTTP_BASE_SCHEMA, **SEARCH_GET_FIELDS}),
    "ma/command": vol.Schema({**_HTTP_BASE_SCHEMA, **MA_COMMAND_FIELDS}),
}
