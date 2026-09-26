"""Authorization policy for Engine WebSocket commands, HTTP views and services.

This module has no Home Assistant imports so the policy can be tested directly. Every
transport (WebSocket, HTTP command view, services) classifies a request with the tables
below and then asks :func:`access_denial` whether the calling user may run it.

Access levels:

- ``read``: any authenticated Home Assistant user.
- ``control``: any authenticated user, but the user needs Home Assistant's ``control``
  entity permission on every player the request targets.
- ``manage``: schedules, timers and volume rules. Administrators only, unless the
  "Allow non-admin users to manage schedules, timers and volume rules" option is on;
  then it behaves like ``control`` (the user must be allowed to control the target
  players).
- ``admin``: administrators only.

Anything that is not classified is refused.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .command_bridge import MUSIC_ASSISTANT_COMMAND_ALLOWLIST

ACCESS_READ = "read"
ACCESS_CONTROL = "control"
ACCESS_MANAGE = "manage"
ACCESS_ADMIN = "admin"
ACCESS_LEVELS = frozenset({ACCESS_READ, ACCESS_CONTROL, ACCESS_MANAGE, ACCESS_ADMIN})

COMMAND_PREFIX = "maverick_music_flow/"

ADMIN_REQUIRED = "Administrator access is required"
MANAGEMENT_ADMIN_REQUIRED = (
    "Administrator access is required to manage schedules, timers and volume rules"
)
TARGET_REQUIRED = "A target player is required"
# Unclassified requests (for example a Music Assistant command outside the allowlist)
# are refused for everyone, administrators included.
NOT_ALLOWED = "This command is not allowed"

# Every WebSocket command the Engine registers, by its name without the domain prefix.
# Commands whose level depends on the payload carry their least privileged level here;
# websocket_access_level() escalates them.
WEBSOCKET_COMMAND_ACCESS: dict[str, str] = {
    # Reads: any authenticated user. diagnostics/run only aggregates data that the
    # individual read commands (schedules/get, volume_rules/get, activity/get, ...)
    # already return, so it stays a read; nothing in it needs administrator access.
    "get_context": ACCESS_READ,
    "bootstrap/get": ACCESS_READ,
    "connections/get": ACCESS_READ,
    "stats/get": ACCESS_READ,
    "playback_stats/get": ACCESS_READ,
    "players/get": ACCESS_READ,
    "diagnostics/run": ACCESS_READ,
    "orchestration/status": ACCESS_READ,
    "queue/get": ACCESS_READ,
    "library/get": ACCESS_READ,
    "favorites/get": ACCESS_READ,
    "search/get": ACCESS_READ,
    "schedules/get": ACCESS_READ,
    "timers/get": ACCESS_READ,
    "volume_rules/get": ACCESS_READ,
    "announcements/get": ACCESS_READ,
    "activity/get": ACCESS_READ,
    "screensaver/get": ACCESS_READ,
    "sendspin/status": ACCESS_READ,
    "lighting/get": ACCESS_READ,
    "interface/get": ACCESS_READ,
    "wheels/get": ACCESS_READ,
    "radio/search": ACCESS_READ,
    # Read without "values"; admin with "values".
    "queue/settings": ACCESS_READ,
    # "list" is a read, "play" is control of selected_player, "save"/"delete" are admin.
    "playlists": ACCESS_READ,
    # Playback control of specific players.
    "playback/play_media": ACCESS_CONTROL,
    "player/command": ACCESS_CONTROL,
    "queue/action": ACCESS_CONTROL,
    "queue/transfer": ACCESS_CONTROL,
    "group/apply": ACCESS_CONTROL,
    "announce": ACCESS_CONTROL,
    # Favorites belong to the shared Music Assistant library but have no player target.
    "favorites/set": ACCESS_CONTROL,
    # Asks open dashboards to show the screensaver once; harmless and not player bound.
    "screensaver/show": ACCESS_CONTROL,
    # Per Music Assistant command, see MUSIC_ASSISTANT_COMMAND_ACCESS.
    "ma/command": ACCESS_CONTROL,
    # scope=user stores the caller's own preference; scope=global is admin.
    "wheels/set": ACCESS_CONTROL,
    # Schedules, timers and volume rules.
    "schedules/set": ACCESS_MANAGE,
    "schedules/delete": ACCESS_MANAGE,
    "schedules/run": ACCESS_MANAGE,
    "timers/set": ACCESS_MANAGE,
    "timers/delete": ACCESS_MANAGE,
    "volume_rules/set": ACCESS_MANAGE,
    "volume_rules/delete": ACCESS_MANAGE,
    "volume_rules/clear": ACCESS_MANAGE,
    # Configuration writes.
    "orchestration/run_once": ACCESS_ADMIN,
    "screensaver/set": ACCESS_ADMIN,
    "lighting/set": ACCESS_ADMIN,
    "interface/set": ACCESS_ADMIN,
}

# The HTTP command view (POST /api/maverick_music_flow/command/{command}).
HTTP_COMMAND_ACCESS: dict[str, str] = {
    "get_context": ACCESS_READ,
    "bootstrap/get": ACCESS_READ,
    "queue/get": ACCESS_READ,
    "library/get": ACCESS_READ,
    "favorites/get": ACCESS_READ,
    "favorites/set": ACCESS_CONTROL,
    "search/get": ACCESS_READ,
    "ma/command": ACCESS_CONTROL,
}

# Home Assistant services. The check only applies to calls that carry a user context;
# automations and scripts run as the system user and are never refused.
SERVICE_ACCESS: dict[str, str] = {
    "set_volume_rule": ACCESS_MANAGE,
    "delete_volume_rule": ACCESS_MANAGE,
    "clear_volume_rules": ACCESS_MANAGE,
    "set_schedule": ACCESS_MANAGE,
    "delete_schedule": ACCESS_MANAGE,
    "run_schedule": ACCESS_MANAGE,
    "set_timer": ACCESS_MANAGE,
    "delete_timer": ACCESS_MANAGE,
    "announce": ACCESS_CONTROL,
    "play_media": ACCESS_CONTROL,
    "player_command": ACCESS_CONTROL,
    "transfer_queue": ACCESS_CONTROL,
    "show_screensaver": ACCESS_CONTROL,
    "run_orchestration": ACCESS_ADMIN,
    "set_screensaver": ACCESS_ADMIN,
    "set_interface_preferences": ACCESS_ADMIN,
    # Registered with Home Assistant's async_register_admin_service.
    "set_queue_settings": ACCESS_ADMIN,
}

# Every allowlisted Music Assistant command, classified. Control commands take their
# targets from the command arguments (see music_assistant_command_targets).
MUSIC_ASSISTANT_COMMAND_ACCESS: dict[str, str] = {
    # Reads
    "players/all": ACCESS_READ,
    "player_queues/all": ACCESS_READ,
    "player_queues/get": ACCESS_READ,
    "player_queues/get_active_queue": ACCESS_READ,
    "player_queues/items": ACCESS_READ,
    "music/search": ACCESS_READ,
    "music/browse": ACCESS_READ,
    "music/item_by_uri": ACCESS_READ,
    "music/recommendations": ACCESS_READ,
    "music/recommendations/items": ACCESS_READ,
    "music/recently_played_items": ACCESS_READ,
    "music/in_progress_items": ACCESS_READ,
    "music/tracks/similar_tracks": ACCESS_READ,
    "music/albums/album_tracks": ACCESS_READ,
    "music/artists/artist_albums": ACCESS_READ,
    "music/artists/artist_tracks": ACCESS_READ,
    "music/playlists/playlist_tracks": ACCESS_READ,
    "music/podcasts/podcast_episodes": ACCESS_READ,
    "music/albums/library_items": ACCESS_READ,
    "music/artists/library_items": ACCESS_READ,
    "music/audiobooks/library_items": ACCESS_READ,
    "music/genres/library_items": ACCESS_READ,
    "music/playlists/library_items": ACCESS_READ,
    "music/podcasts/library_items": ACCESS_READ,
    "music/radios/library_items": ACCESS_READ,
    "music/tracks/library_items": ACCESS_READ,
    "metadata/get_track_lyrics": ACCESS_READ,
    "audio_analysis/wave_form": ACCESS_READ,
    "ai_radio/hosts/list": ACCESS_READ,
    "ai_radio/queue_dj/status": ACCESS_READ,
    # Playback control of the players or queues named in the arguments
    "players/cmd/play": ACCESS_CONTROL,
    "players/cmd/pause": ACCESS_CONTROL,
    "players/cmd/stop": ACCESS_CONTROL,
    "players/cmd/next": ACCESS_CONTROL,
    "players/cmd/previous": ACCESS_CONTROL,
    "players/cmd/volume_set": ACCESS_CONTROL,
    "players/cmd/volume_mute": ACCESS_CONTROL,
    "players/cmd/group_many": ACCESS_CONTROL,
    "players/cmd/set_members": ACCESS_CONTROL,
    "players/cmd/ungroup": ACCESS_CONTROL,
    "player_queues/play_media": ACCESS_CONTROL,
    "player_queues/play_index": ACCESS_CONTROL,
    "player_queues/shuffle": ACCESS_CONTROL,
    "player_queues/repeat": ACCESS_CONTROL,
    "player_queues/crossfade": ACCESS_CONTROL,
    "player_queues/autoplay": ACCESS_CONTROL,
    "player_queues/seek": ACCESS_CONTROL,
    "player_queues/set_playback_speed": ACCESS_CONTROL,
    "player_queues/move_item": ACCESS_CONTROL,
    "player_queues/delete_item": ACCESS_CONTROL,
    "player_queues/clear": ACCESS_CONTROL,
    "player_queues/transfer": ACCESS_CONTROL,
    "ai_radio/queue_dj/set": ACCESS_CONTROL,
    # Favorites: same level as the Engine's own favorites/set command
    "music/favorites/add_item": ACCESS_CONTROL,
    "music/favorites/remove_item": ACCESS_CONTROL,
    # Shared library writes: configuration
    "music/library/add_item": ACCESS_ADMIN,
    "music/playlists/add_playlist_tracks": ACCESS_ADMIN,
}

# Music Assistant commands that always name the players or queues they act on. A
# control-level call without a recognizable target is refused for non-administrators
# instead of being passed through unchecked.
MUSIC_ASSISTANT_TARGET_KEYS = (
    "player_id",
    "queue_id",
    "target_player",
    "player_ids",
    "child_player_ids",
    "player_ids_to_add",
    "player_ids_to_remove",
    "source_queue_id",
    "target_queue_id",
)

_MANAGE_COMMANDS = frozenset(
    name for name, level in WEBSOCKET_COMMAND_ACCESS.items() if level == ACCESS_MANAGE
)


@dataclass(frozen=True, slots=True)
class AccessDenial:
    """Why a request was refused."""

    reason: str
    entity_id: str = ""


def _clean(value: Any) -> str:
    """Return a stripped string, or '' for anything that is not a non-empty string."""
    return value.strip() if isinstance(value, str) else ""


def _first(payload: dict[str, Any], *keys: str) -> list[str]:
    """Return the first non-empty value among keys, mirroring the runtime's precedence."""
    for key in keys:
        clean = _clean(payload.get(key))
        if clean:
            return [clean]
    return []


def _many(payload: dict[str, Any], *keys: str) -> list[str]:
    """Return every non-empty string found under the given list-valued keys."""
    found: list[str] = []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            continue
        found.extend(clean for clean in (_clean(item) for item in value) if clean)
    return found


def _unique(values: Iterable[str]) -> list[str]:
    """Return values without duplicates, keeping order."""
    return list(dict.fromkeys(values))


def command_name(command_type: str) -> str:
    """Return a WebSocket command type without the domain prefix."""
    clean = _clean(command_type)
    return clean[len(COMMAND_PREFIX) :] if clean.startswith(COMMAND_PREFIX) else clean


def music_assistant_command_access(command: str) -> str:
    """Return the access level of a Music Assistant command, or '' when it is not allowed."""
    clean = _clean(command)
    if clean not in MUSIC_ASSISTANT_COMMAND_ALLOWLIST:
        return ""
    return MUSIC_ASSISTANT_COMMAND_ACCESS.get(clean, "")


def music_assistant_command_targets(args: Any) -> list[str]:
    """Return the player and queue identifiers a Music Assistant command acts on."""
    if not isinstance(args, dict):
        return []
    targets: list[str] = []
    for key in MUSIC_ASSISTANT_TARGET_KEYS:
        targets += _first(args, key) + _many(args, key)
    return _unique(targets)


def websocket_access_level(command_type: str, payload: dict[str, Any]) -> str:
    """Return the access level a WebSocket message needs, or '' when unclassified."""
    name = command_name(command_type)
    level = WEBSOCKET_COMMAND_ACCESS.get(name, "")
    if name == "queue/settings" and "values" in payload:
        return ACCESS_ADMIN
    if name == "playlists":
        action = _clean(payload.get("action")) or "list"
        if action == "play":
            return ACCESS_CONTROL
        if action in {"save", "delete"}:
            return ACCESS_ADMIN
        return ACCESS_READ
    if name == "wheels/set" and _clean(payload.get("scope")) == "global":
        return ACCESS_ADMIN
    if name == "ma/command":
        return music_assistant_command_access(payload.get("command"))
    return level


def http_access_level(command: str, payload: dict[str, Any]) -> str:
    """Return the access level an HTTP command view request needs, or '' when unclassified."""
    clean = _clean(command)
    if clean == "ma/command":
        return music_assistant_command_access(payload.get("command"))
    return HTTP_COMMAND_ACCESS.get(clean, "")


def service_access_level(service: str) -> str:
    """Return the access level a service needs, or '' when unclassified."""
    return SERVICE_ACCESS.get(_clean(service), "")


def payload_targets(name: str, payload: dict[str, Any]) -> list[str]:
    """Return the raw player identifiers a command or service payload targets.

    ``name`` is a WebSocket command without prefix, an HTTP view command or a service
    name. Key precedence mirrors the runtime method that finally runs the request.
    """
    if name in {"playback/play_media", "player/command", "queue/action", "play_media",
                "player_command"}:
        return _first(payload, "player", "entity_id", "selected_player")
    if name in {"queue/transfer", "transfer_queue"}:
        return _unique(
            _first(payload, "source_player", "source_entity_id")
            + _first(payload, "target_player", "target_entity_id", "entity_id", "selected_player")
        )
    if name == "group/apply":
        return _unique(
            _first(payload, "owner", "entity_id") + _many(payload, "members", "remove_members")
        )
    if name == "announce":
        return _unique(_first(payload, "player", "entity_id") + _many(payload, "players"))
    if name == "playlists":
        if (_clean(payload.get("action")) or "list") == "play":
            return _first(payload, "selected_player")
        return []
    if name == "ma/command":
        return music_assistant_command_targets(payload.get("args"))
    if name in _MANAGE_COMMANDS or service_access_level(name) == ACCESS_MANAGE:
        return _first(payload, "player", "entity_id")
    return []


def stored_targets(
    name: str,
    payload: dict[str, Any],
    *,
    schedules: Iterable[dict[str, Any]] = (),
    timers: Iterable[dict[str, Any]] = (),
    volume_rules: Iterable[dict[str, Any]] = (),
    default_profile_id: str = "default",
) -> list[str]:
    """Return the players of stored items that a delete/run/clear request refers to.

    The lists should already be filtered to the request's profile. Requests that name
    the player directly are covered by payload_targets().
    """
    profile_id = _clean(payload.get("profile_id")) or default_profile_id
    item_id = _clean(payload.get("id")) or _clean(payload.get("schedule_id")) or _clean(
        payload.get("timer_id")
    )

    def players(items: Iterable[dict[str, Any]], *, match_id: bool) -> list[str]:
        found: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if (_clean(item.get("profile_id")) or default_profile_id) != profile_id:
                continue
            if match_id and _clean(item.get("id") or item.get("schedule_id")) != item_id:
                continue
            player = _clean(item.get("player") or item.get("entity_id"))
            if player:
                found.append(player)
        return found

    if name in {"schedules/delete", "schedules/run", "delete_schedule", "run_schedule"}:
        return _unique(players(schedules, match_id=True)) if item_id else []
    if name in {"timers/delete", "delete_timer"}:
        return _unique(players(timers, match_id=True)) if item_id else []
    if name in {"volume_rules/clear", "clear_volume_rules"}:
        return _unique(players(volume_rules, match_id=False))
    return []


def requires_target(name: str, level: str) -> bool:
    """Return whether a control request must name at least one player to be allowed."""
    return name == "ma/command" and level == ACCESS_CONTROL


def access_denial(
    level: str,
    *,
    is_admin: bool,
    can_control: Callable[[str], bool],
    targets: Iterable[str] = (),
    require_target: bool = False,
    management_allowed: bool = False,
) -> AccessDenial | None:
    """Return why the request is refused, or None when the user may run it.

    ``targets`` are Home Assistant entity ids. ``can_control`` answers whether the user
    holds the ``control`` entity permission for one of them. Unknown levels are refused.
    """
    if level == ACCESS_READ:
        return None
    if level == ACCESS_ADMIN:
        return None if is_admin else AccessDenial(ADMIN_REQUIRED)
    if level == ACCESS_MANAGE:
        if is_admin:
            return None
        if not management_allowed:
            return AccessDenial(MANAGEMENT_ADMIN_REQUIRED)
    elif level != ACCESS_CONTROL:
        return AccessDenial(NOT_ALLOWED)
    clean_targets = _unique(clean for clean in (_clean(target) for target in targets) if clean)
    if require_target and not clean_targets and not is_admin:
        return AccessDenial(TARGET_REQUIRED)
    for entity_id in clean_targets:
        if not can_control(entity_id):
            return AccessDenial(f"No permission to control {entity_id}", entity_id)
    return None
