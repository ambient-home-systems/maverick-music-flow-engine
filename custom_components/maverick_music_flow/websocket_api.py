"""WebSocket API for HOMEii Flow Engine."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components import websocket_api
from homeassistant.components.websocket_api import ActiveConnection
from homeassistant.core import HomeAssistant, callback

from .authorization import (
    ACCESS_MANAGE,
    access_denial,
    command_name,
    payload_targets,
    requires_target,
    stored_targets,
    websocket_access_level,
)
from .command_bridge import (
    BASE_SCHEMA,
    FAVORITES_GET_FIELDS,
    FAVORITES_SET_FIELDS,
    LIBRARY_GET_FIELDS,
    MA_COMMAND_FIELDS,
    QUEUE_GET_FIELDS,
    SEARCH_GET_FIELDS,
    strip_internal_keys,
)
from .const import CONF_INSTANCE_ID, CONF_PROFILE_ID, DEFAULT_PROFILE_ID, DOMAIN
from .runtime import HomeiiFlowRuntime
from .radio_directory import search_stations
from .saved_playlists import list_playlists, save_playlist, play_playlist, delete_playlist
from .interface_preferences import read_preferences, save_preferences, read_wheel_preferences, save_wheel_preferences


def _runtime(hass: HomeAssistant) -> HomeiiFlowRuntime:
    """Return the shared runtime."""
    return hass.data[DOMAIN]["runtime"]


def _command_payload(msg: dict[str, Any]) -> dict[str, Any]:
    """Return command data without the websocket message id, type or internal flags."""
    payload = strip_internal_keys(msg)
    payload.pop("id", None)
    payload.pop("type", None)
    return payload


def _authorize(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> bool:
    """Return whether the connected user may run msg; send ERR_UNAUTHORIZED otherwise.

    Every handler calls this first. The level comes from the authorization tables, so an
    unclassified command is refused. Player targets are mapped to their media_player
    entity and checked against the user's Home Assistant entity permissions.
    """
    runtime = _runtime(hass)
    payload = _command_payload(msg)
    name = command_name(msg["type"])
    level = websocket_access_level(msg["type"], payload)
    targets = payload_targets(name, payload)
    if level == ACCESS_MANAGE:
        profile_id = str(payload.get(CONF_PROFILE_ID) or DEFAULT_PROFILE_ID)
        targets += stored_targets(
            name,
            payload,
            schedules=runtime.schedules(profile_id),
            timers=runtime.timers(profile_id),
            volume_rules=runtime.volume_rules(profile_id),
            default_profile_id=DEFAULT_PROFILE_ID,
        )
    user = connection.user
    denial = access_denial(
        level,
        is_admin=bool(user.is_admin),
        can_control=lambda entity_id: bool(user.permissions.check_entity(entity_id, POLICY_CONTROL)),
        targets=[runtime.control_entity_id(target) for target in targets],
        require_target=requires_target(name, level),
        management_allowed=runtime.non_admin_management_allowed(payload.get(CONF_INSTANCE_ID)),
    )
    if denial is None:
        return True
    connection.send_error(msg["id"], websocket_api.ERR_UNAUTHORIZED, denial.reason)
    return False


def async_register_websocket_commands(hass: HomeAssistant) -> None:
    """Register HOMEii Flow Engine websocket commands."""
    websocket_api.async_register_command(hass, websocket_saved_playlists)
    websocket_api.async_register_command(hass, websocket_get_wheel_preferences)
    websocket_api.async_register_command(hass, websocket_set_wheel_preferences)
    websocket_api.async_register_command(hass, websocket_get_interface_preferences)
    websocket_api.async_register_command(hass, websocket_set_interface_preferences)
    websocket_api.async_register_command(hass, websocket_get_artwork_lighting)
    websocket_api.async_register_command(hass, websocket_set_artwork_lighting)
    websocket_api.async_register_command(hass, websocket_radio_search)
    websocket_api.async_register_command(hass, websocket_get_context)
    websocket_api.async_register_command(hass, websocket_get_bootstrap)
    websocket_api.async_register_command(hass, websocket_get_required_connections)
    websocket_api.async_register_command(hass, websocket_run_diagnostics)
    websocket_api.async_register_command(hass, websocket_get_stats)
    websocket_api.async_register_command(hass, websocket_get_playback_stats)
    websocket_api.async_register_command(hass, websocket_get_players)
    websocket_api.async_register_command(hass, websocket_get_orchestration_status)
    websocket_api.async_register_command(hass, websocket_run_orchestration_once)
    websocket_api.async_register_command(hass, websocket_play_media)
    websocket_api.async_register_command(hass, websocket_player_command)
    websocket_api.async_register_command(hass, websocket_music_assistant_command)
    websocket_api.async_register_command(hass, websocket_queue_settings)
    websocket_api.async_register_command(hass, websocket_get_queue)
    websocket_api.async_register_command(hass, websocket_queue_action)
    websocket_api.async_register_command(hass, websocket_transfer_queue)
    websocket_api.async_register_command(hass, websocket_get_library)
    websocket_api.async_register_command(hass, websocket_get_favorites)
    websocket_api.async_register_command(hass, websocket_set_favorite)
    websocket_api.async_register_command(hass, websocket_get_search)
    websocket_api.async_register_command(hass, websocket_apply_group)
    websocket_api.async_register_command(hass, websocket_get_schedules)
    websocket_api.async_register_command(hass, websocket_set_schedule)
    websocket_api.async_register_command(hass, websocket_delete_schedule)
    websocket_api.async_register_command(hass, websocket_run_schedule)
    websocket_api.async_register_command(hass, websocket_get_timers)
    websocket_api.async_register_command(hass, websocket_set_timer)
    websocket_api.async_register_command(hass, websocket_delete_timer)
    websocket_api.async_register_command(hass, websocket_get_volume_rules)
    websocket_api.async_register_command(hass, websocket_set_volume_rule)
    websocket_api.async_register_command(hass, websocket_delete_volume_rule)
    websocket_api.async_register_command(hass, websocket_clear_volume_rules)
    websocket_api.async_register_command(hass, websocket_get_announcements)
    websocket_api.async_register_command(hass, websocket_announce)
    websocket_api.async_register_command(hass, websocket_get_activity)
    websocket_api.async_register_command(hass, websocket_get_screensaver)
    websocket_api.async_register_command(hass, websocket_set_screensaver)
    websocket_api.async_register_command(hass, websocket_show_screensaver)
    websocket_api.async_register_command(hass, websocket_sendspin_status)


@websocket_api.websocket_command({vol.Required("type"): "maverick_music_flow/get_context", **BASE_SCHEMA})
@callback
def websocket_get_context(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    """Return Engine context."""
    if not _authorize(hass, connection, msg):
        return
    result = _runtime(hass).context(
        instance_id=msg.get(CONF_INSTANCE_ID),
        profile_id=msg.get(CONF_PROFILE_ID),
    )
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): "maverick_music_flow/bootstrap/get", **BASE_SCHEMA})
@websocket_api.async_response
async def websocket_get_bootstrap(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    """Return a coherent startup snapshot in one Home Assistant round trip."""
    if not _authorize(hass, connection, msg):
        return
    try:
        result = await _runtime(hass).async_bootstrap_snapshot(
            instance_id=msg.get(CONF_INSTANCE_ID),
            profile_id=msg.get(CONF_PROFILE_ID),
        )
        connection.send_result(msg["id"], result)
    except Exception as err:  # noqa: BLE001 - handshake failures must be explicit
        connection.send_error(msg["id"], "bootstrap_failed", str(err))


@websocket_api.websocket_command({vol.Required("type"): "maverick_music_flow/connections/get", **BASE_SCHEMA})
@callback
def websocket_get_required_connections(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    """Return required Engine connection health."""
    if not _authorize(hass, connection, msg):
        return
    connection.send_result(msg["id"], _runtime(hass).required_connections_snapshot())


@websocket_api.websocket_command({vol.Required("type"): "maverick_music_flow/stats/get", **BASE_SCHEMA})
@callback
def websocket_get_stats(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    """Return Engine stats."""
    if not _authorize(hass, connection, msg):
        return
    connection.send_result(msg["id"], _runtime(hass).stats())


@websocket_api.websocket_command({vol.Required("type"): "maverick_music_flow/playback_stats/get", **BASE_SCHEMA})
@callback
def websocket_get_playback_stats(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    """Return passive playback statistics."""
    if not _authorize(hass, connection, msg):
        return
    connection.send_result(msg["id"], _runtime(hass).playback_statistics())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "maverick_music_flow/players/get",
        **BASE_SCHEMA,
        vol.Optional("include_all", default=False): bool,
        vol.Optional("include_generic", default=False): bool,
    }
)
@websocket_api.async_response
async def websocket_get_players(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    """Return Engine player state."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).async_players_snapshot())
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "players_failed", str(err))


@websocket_api.websocket_command({vol.Required("type"): "maverick_music_flow/diagnostics/run", **BASE_SCHEMA})
@callback
def websocket_run_diagnostics(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return Engine diagnostics."""
    if not _authorize(hass, connection, msg):
        return
    runtime = _runtime(hass)
    connection.send_result(
        msg["id"],
        {
            "context": runtime.context(
                instance_id=msg.get(CONF_INSTANCE_ID),
                profile_id=msg.get(CONF_PROFILE_ID),
            ),
            "required_connections": runtime.required_connections_snapshot(),
            "stats": runtime.stats(),
            "playback_statistics": runtime.playback_statistics(),
            "screensaver": runtime.screensaver_state(msg.get(CONF_PROFILE_ID)),
            "orchestration": runtime.orchestration_status(),
            "schedules": runtime.schedules(msg.get(CONF_PROFILE_ID)),
            "timers": runtime.timers(msg.get(CONF_PROFILE_ID)),
            "volume_rules": runtime.volume_rules(msg.get(CONF_PROFILE_ID)),
            "volume_rule_summaries": runtime.volume_rule_summaries(msg.get(CONF_PROFILE_ID)),
            "active_volume_rules": runtime.active_volume_rule_summaries(msg.get(CONF_PROFILE_ID)),
            "announcements": runtime.announcements(msg.get(CONF_PROFILE_ID)),
            "activity": runtime.activity(msg.get(CONF_PROFILE_ID))[:10],
        },
    )


@websocket_api.websocket_command({vol.Required("type"): "maverick_music_flow/orchestration/status", **BASE_SCHEMA})
@callback
def websocket_get_orchestration_status(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return Engine orchestration status."""
    if not _authorize(hass, connection, msg):
        return
    connection.send_result(msg["id"], _runtime(hass).orchestration_status())


@websocket_api.websocket_command({vol.Required("type"): "maverick_music_flow/orchestration/run_once", **BASE_SCHEMA})
@websocket_api.async_response
async def websocket_run_orchestration_once(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Run one Engine orchestration pass."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).async_tick_orchestration())
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "orchestration_run_failed", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "maverick_music_flow/playback/play_media",
        **BASE_SCHEMA,
        vol.Optional("player"): str,
        vol.Optional("entity_id"): str,
        vol.Optional("media_id"): str,
        vol.Optional("media_content_id"): str,
        vol.Optional("uri"): str,
        vol.Optional("media_type"): str,
        vol.Optional("media_content_type"): str,
        vol.Optional("enqueue", default="play"): str,
        vol.Optional("radio_mode", default=False): bool,
        vol.Optional("verify_playback", default=False): bool,
        vol.Optional("media_name"): str,
        vol.Optional("playlist_name"): str,
    }
)
@websocket_api.async_response
async def websocket_play_media(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Play media through the Engine proxy."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).async_play_media(_command_payload(msg)))
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "playback_failed", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "maverick_music_flow/player/command",
        **BASE_SCHEMA,
        vol.Optional("player"): str,
        vol.Optional("entity_id"): str,
        vol.Required("command"): str,
        vol.Optional("action"): str,
        vol.Optional("volume"): vol.Any(int, float),
        vol.Optional("volume_level"): vol.Any(int, float),
        vol.Optional("position"): vol.Any(int, float),
        vol.Optional("seek_position"): vol.Any(int, float),
        vol.Optional("speed"): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=3.0)),
        vol.Optional("shuffle", default=True): bool,
        vol.Optional("repeat"): str,
        vol.Optional("is_volume_muted"): bool,
        vol.Optional("autoplay_enabled"): bool,
        vol.Optional("crossfade_enabled"): bool,
        vol.Optional("enabled"): bool,
    }
)
@websocket_api.async_response
async def websocket_player_command(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Run a media player command through the Engine proxy."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).async_player_command(_command_payload(msg)))
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "player_command_failed", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "maverick_music_flow/ma/command",
        **BASE_SCHEMA,
        **MA_COMMAND_FIELDS,
    }
)
@websocket_api.async_response
async def websocket_music_assistant_command(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Run a Music Assistant command through the Engine server bridge."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).async_music_assistant_command(_command_payload(msg)))
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "music_assistant_command_failed", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "maverick_music_flow/queue/get",
        **BASE_SCHEMA,
        **QUEUE_GET_FIELDS,
    }
)
@websocket_api.async_response
async def websocket_get_queue(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return queue data."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).async_get_queue(_command_payload(msg)))
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "queue_failed", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "maverick_music_flow/queue/action",
        **BASE_SCHEMA,
        vol.Optional("entity_id"): str,
        vol.Optional("player"): str,
        vol.Optional("queue_id"): str,
        vol.Required("action"): str,
        vol.Optional("queue_item_id"): vol.Any(str, int),
        vol.Optional("item_id"): vol.Any(str, int),
        vol.Optional("position"): vol.Any(str, int),
        vol.Optional("position_shift"): vol.Any(str, int),
    }
)
@websocket_api.async_response
async def websocket_queue_action(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Run a queue item action through the Engine."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).async_queue_action(_command_payload(msg)))
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "queue_action_failed", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "maverick_music_flow/queue/transfer",
        **BASE_SCHEMA,
        vol.Optional("source_player"): str,
        vol.Optional("source_entity_id"): str,
        vol.Optional("target_player"): str,
        vol.Optional("target_entity_id"): str,
        vol.Optional("entity_id"): str,
        vol.Optional("auto_play", default=True): bool,
    }
)
@websocket_api.async_response
async def websocket_transfer_queue(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Transfer a Music Assistant queue through the Engine proxy."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).async_transfer_queue(_command_payload(msg)))
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "queue_transfer_failed", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "maverick_music_flow/library/get",
        **BASE_SCHEMA,
        **LIBRARY_GET_FIELDS,
    }
)
@websocket_api.async_response
async def websocket_get_library(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return library data."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).async_get_library(_command_payload(msg)))
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "library_failed", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "maverick_music_flow/favorites/get",
        **BASE_SCHEMA,
        **FAVORITES_GET_FIELDS,
    }
)
@websocket_api.async_response
async def websocket_get_favorites(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return one aggregated Music Assistant favorites snapshot."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).async_get_favorites(_command_payload(msg)))
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "favorites_failed", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "maverick_music_flow/favorites/set",
        **BASE_SCHEMA,
        **FAVORITES_SET_FIELDS,
    }
)
@websocket_api.async_response
async def websocket_set_favorite(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Add or remove a Music Assistant favorite through the Engine."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).async_set_favorite(_command_payload(msg)))
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "favorite_mutation_failed", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "maverick_music_flow/search/get",
        **BASE_SCHEMA,
        **SEARCH_GET_FIELDS,
    }
)
@websocket_api.async_response
async def websocket_get_search(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return Music Assistant search data."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).async_get_search(_command_payload(msg)))
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "search_failed", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "maverick_music_flow/group/apply",
        **BASE_SCHEMA,
        vol.Optional("owner"): str,
        vol.Optional("entity_id"): str,
        vol.Optional("members", default=[]): [str],
        vol.Optional("remove_members", default=[]): [str],
        vol.Optional("clear_all", default=False): bool,
    }
)
@websocket_api.async_response
async def websocket_apply_group(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Apply group changes."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).async_apply_group(_command_payload(msg)))
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "group_apply_failed", str(err))


@websocket_api.websocket_command({vol.Required("type"): "maverick_music_flow/schedules/get", **BASE_SCHEMA})
@callback
def websocket_get_schedules(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    """Return stored schedules."""
    if not _authorize(hass, connection, msg):
        return
    runtime = _runtime(hass)
    profile_id = msg.get(CONF_PROFILE_ID)
    connection.send_result(
        msg["id"],
        {
            "schedules": runtime.schedules(profile_id),
            "summaries": runtime.schedule_summaries(profile_id),
            "next_schedule": runtime.next_schedule_summary(profile_id),
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "maverick_music_flow/schedules/set",
        **BASE_SCHEMA,
        vol.Optional("schedule_id"): str,
        vol.Optional("name"): str,
        vol.Optional("kind"): str,
        vol.Optional("action"): str,
        vol.Optional("player"): str,
        vol.Optional("entity_id"): str,
        vol.Optional("media_id"): str,
        vol.Optional("media_content_id"): str,
        vol.Optional("playlist"): str,
        vol.Optional("media_type"): str,
        vol.Optional("media_content_type"): str,
        vol.Optional("media_name"): str,
        vol.Optional("playlist_name"): str,
        vol.Optional("media_mode"): str,
        vol.Optional("selection_mode"): str,
        vol.Optional("enqueue"): str,
        vol.Optional("radio_mode"): bool,
        vol.Optional("retry_attempts"): int,
        vol.Optional("retry_delay"): int,
        vol.Optional("time"): str,
        vol.Optional("days", default=list): [int],
        vol.Optional("volume"): int,
        vol.Optional("enabled", default=True): bool,
        vol.Optional("after_run"): str,
        vol.Optional("afterRun"): str,
    }
)
@websocket_api.async_response
async def websocket_set_schedule(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Store a schedule."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).async_set_schedule(_command_payload(msg)))
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "schedule_set_failed", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "maverick_music_flow/schedules/delete",
        **BASE_SCHEMA,
        vol.Optional("schedule_id"): str,
    }
)
@websocket_api.async_response
async def websocket_delete_schedule(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Delete a schedule."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).async_delete_schedule(_command_payload(msg)))
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "schedule_delete_failed", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "maverick_music_flow/schedules/run",
        **BASE_SCHEMA,
        vol.Optional("id"): str,
        vol.Optional("schedule_id"): str,
    }
)
@websocket_api.async_response
async def websocket_run_schedule(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Run a stored schedule immediately."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).async_run_schedule_now(_command_payload(msg)))
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "schedule_run_failed", str(err))


@websocket_api.websocket_command({vol.Required("type"): "maverick_music_flow/timers/get", **BASE_SCHEMA})
@callback
def websocket_get_timers(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    """Return stored timers."""
    if not _authorize(hass, connection, msg):
        return
    runtime = _runtime(hass)
    profile_id = msg.get(CONF_PROFILE_ID)
    connection.send_result(
        msg["id"],
        {
            "timers": runtime.timers(profile_id),
            "summaries": runtime.timer_summaries(profile_id),
            "next_timer": runtime.next_timer_summary(profile_id),
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "maverick_music_flow/timers/set",
        **BASE_SCHEMA,
        vol.Optional("timer_id"): str,
        vol.Optional("timer_type"): str,
        vol.Optional("kind"): str,
        vol.Optional("player"): str,
        vol.Optional("entity_id"): str,
        vol.Optional("action", default="stop"): str,
        vol.Optional("minutes"): int,
        vol.Optional("ends_at"): str,
        vol.Optional("target_at"): str,
        vol.Optional("origin"): str,
        vol.Optional("enabled", default=True): bool,
    }
)
@websocket_api.async_response
async def websocket_set_timer(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Store a timer."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).async_set_timer(_command_payload(msg)))
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "timer_set_failed", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "maverick_music_flow/timers/delete",
        **BASE_SCHEMA,
        vol.Optional("timer_id"): str,
        vol.Optional("player"): str,
        vol.Optional("entity_id"): str,
    }
)
@websocket_api.async_response
async def websocket_delete_timer(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Delete a timer."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).async_delete_timer(_command_payload(msg)))
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "timer_delete_failed", str(err))


@websocket_api.websocket_command({vol.Required("type"): "maverick_music_flow/volume_rules/get", **BASE_SCHEMA})
@callback
def websocket_get_volume_rules(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    """Return stored volume rules."""
    if not _authorize(hass, connection, msg):
        return
    runtime = _runtime(hass)
    profile_id = msg.get(CONF_PROFILE_ID)
    connection.send_result(
        msg["id"],
        {
            "volume_rules": runtime.volume_rules(profile_id),
            "summaries": runtime.volume_rule_summaries(profile_id),
            "active": runtime.active_volume_rule_summaries(profile_id),
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "maverick_music_flow/volume_rules/set",
        **BASE_SCHEMA,
        vol.Required("player"): str,
        vol.Required("max_volume"): int,
        vol.Optional("start_time"): str,
        vol.Optional("end_time"): str,
        vol.Optional("days", default=list): [int],
        vol.Optional("enabled", default=True): bool,
    }
)
@websocket_api.async_response
async def websocket_set_volume_rule(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Store a volume rule."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).async_set_volume_rule(_command_payload(msg)))
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "volume_rule_failed", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "maverick_music_flow/volume_rules/delete",
        **BASE_SCHEMA,
        vol.Required("player"): str,
    }
)
@websocket_api.async_response
async def websocket_delete_volume_rule(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Delete one volume rule."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).async_delete_volume_rule(_command_payload(msg)))
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "volume_rule_delete_failed", str(err))


@websocket_api.websocket_command({vol.Required("type"): "maverick_music_flow/volume_rules/clear", **BASE_SCHEMA})
@websocket_api.async_response
async def websocket_clear_volume_rules(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Clear volume rules."""
    if not _authorize(hass, connection, msg):
        return
    try:
        profile_id = str(msg.get(CONF_PROFILE_ID) or "default")
        connection.send_result(msg["id"], await _runtime(hass).async_clear_volume_rules(profile_id))
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "volume_rules_clear_failed", str(err))


@websocket_api.websocket_command({vol.Required("type"): "maverick_music_flow/announcements/get", **BASE_SCHEMA})
@callback
def websocket_get_announcements(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    """Return recorded announcements."""
    if not _authorize(hass, connection, msg):
        return
    connection.send_result(msg["id"], {"announcements": _runtime(hass).announcements(msg.get(CONF_PROFILE_ID))})


@websocket_api.websocket_command({vol.Required("type"): "maverick_music_flow/activity/get", **BASE_SCHEMA})
@callback
def websocket_get_activity(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    """Return recent Engine activity."""
    if not _authorize(hass, connection, msg):
        return
    runtime = _runtime(hass)
    profile_id = msg.get(CONF_PROFILE_ID)
    connection.send_result(
        msg["id"],
        {
            "activity": runtime.activity(profile_id),
            "last_activity": runtime.last_activity(profile_id),
            "count": runtime.activity_count(profile_id),
        },
    )


@websocket_api.websocket_command({vol.Required("type"): "maverick_music_flow/screensaver/get", **BASE_SCHEMA})
@callback
def websocket_get_screensaver(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    """Return system-wide screensaver configuration and state."""
    if not _authorize(hass, connection, msg):
        return
    connection.send_result(msg["id"], _runtime(hass).screensaver_state(msg.get(CONF_PROFILE_ID)))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "maverick_music_flow/screensaver/set",
        **BASE_SCHEMA,
        vol.Optional("enabled"): bool,
        vol.Optional("timeout_seconds"): int,
        vol.Optional("mode"): str,
        vol.Optional("auto_lyrics_when_playing"): bool,
        vol.Optional("clock_mode"): str,
        vol.Optional("message"): str,
        vol.Optional("show_artwork"): bool,
        vol.Optional("music_assistant_url"): str,
        vol.Optional("ma_url"): str,
        vol.Optional("music_assistant_external_url"): str,
    }
)
@websocket_api.async_response
async def websocket_set_screensaver(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Store system-wide screensaver configuration."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).async_set_screensaver_config(_command_payload(msg)))
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "screensaver_set_failed", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "maverick_music_flow/screensaver/show",
        **BASE_SCHEMA,
    }
)
@websocket_api.async_response
async def websocket_show_screensaver(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Request loaded frontend screensaver agents to open immediately."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).async_request_screensaver_show(_command_payload(msg)))
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "screensaver_show_failed", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "maverick_music_flow/announce",
        **BASE_SCHEMA,
        vol.Required("message"): str,
        vol.Optional("player"): str,
        vol.Optional("players", default=list): [str],
        vol.Optional("entity_id"): str,
        vol.Optional("volume"): int,
        vol.Optional("language"): str,
        vol.Optional("tts_entity"): str,
        vol.Optional("announcement_tts_entity"): str,
        vol.Optional("target"): str,
        vol.Optional("sent", default=True): bool,
    }
)
@websocket_api.async_response
async def websocket_announce(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Send and record an announcement request."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).async_send_announcement(_command_payload(msg)))
    except Exception as err:  # noqa: BLE001 - surfaced to frontend diagnostics
        connection.send_error(msg["id"], "announce_failed", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "maverick_music_flow/sendspin/status",
        **BASE_SCHEMA,
        vol.Optional("player_id"): str,
    }
)
@callback
def websocket_sendspin_status(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return Sendspin status."""
    if not _authorize(hass, connection, msg):
        return
    connection.send_result(msg["id"], _runtime(hass).sendspin_status(_command_payload(msg)))


@websocket_api.websocket_command({
    vol.Required("type"): "maverick_music_flow/queue/settings", **BASE_SCHEMA,
    vol.Optional("values"): dict,
})
@websocket_api.async_response
async def websocket_queue_settings(hass, connection, msg):
    """Read shared queue preferences; only HA administrators may change them."""
    if not _authorize(hass, connection, msg):
        return
    try:
        result = await _runtime(hass).async_queue_settings(_command_payload(msg))
        result["can_edit"] = connection.user.is_admin
        connection.send_result(msg["id"], result)
    except Exception as err:  # noqa: BLE001 - explicit command failure
        connection.send_error(msg["id"], "queue_settings_failed", str(err))


@websocket_api.websocket_command({vol.Required("type"): "maverick_music_flow/lighting/get", **BASE_SCHEMA})
@callback
def websocket_get_artwork_lighting(hass, connection, msg):
    """Return persistent player/light assignments and their current status."""
    if not _authorize(hass, connection, msg):
        return
    connection.send_result(msg["id"], _runtime(hass).artwork_lighting.snapshot())


@websocket_api.websocket_command({
    vol.Required("type"): "maverick_music_flow/lighting/set", **BASE_SCHEMA,
    vol.Required("player"): str, vol.Optional("lights"): [str],
    vol.Optional("enabled"): bool, vol.Optional("brightness"): vol.Coerce(float),
    vol.Optional("transition"): vol.Coerce(float), vol.Optional("cooldown"): vol.Coerce(float),
})
@websocket_api.async_response
async def websocket_set_artwork_lighting(hass, connection, msg):
    """Persist and apply a player's artwork lighting configuration."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await _runtime(hass).artwork_lighting.configure(_command_payload(msg)))
    except Exception as err:
        connection.send_error(msg["id"], "lighting_failed", str(err))


@websocket_api.websocket_command({vol.Required("type"): "maverick_music_flow/radio/search", **BASE_SCHEMA,
    vol.Optional("query",default=""): str, vol.Optional("country",default=""): str,
    vol.Optional("tag",default=""): str, vol.Optional("limit",default=40): vol.All(vol.Coerce(int),vol.Range(min=8,max=80))})
@websocket_api.async_response
async def websocket_radio_search(hass, connection, msg):
    """Search the public station directory, preserving artwork through the Engine."""
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await search_stations(_runtime(hass), _command_payload(msg)))
    except Exception as err:
        connection.send_error(msg["id"], "radio_search_failed", str(err))


@websocket_api.websocket_command({vol.Required("type"): "maverick_music_flow/interface/get", **BASE_SCHEMA})
@callback
def websocket_get_interface_preferences(hass, connection, msg):
    if not _authorize(hass, connection, msg):
        return
    connection.send_result(msg["id"], read_preferences(_runtime(hass), msg.get("profile_id")))


@websocket_api.websocket_command({vol.Required("type"): "maverick_music_flow/interface/set", **BASE_SCHEMA,
    vol.Optional("night_mode"): str, vol.Optional("night_start"): str,
    vol.Optional("night_end"): str, vol.Optional("night_days"): [int]})
@websocket_api.async_response
async def websocket_set_interface_preferences(hass, connection, msg):
    if not _authorize(hass, connection, msg):
        return
    try:
        connection.send_result(msg["id"], await save_preferences(_runtime(hass), _command_payload(msg)))
    except Exception as err:
        connection.send_error(msg["id"], "interface_set_failed", str(err))


@websocket_api.websocket_command({vol.Required("type"): "maverick_music_flow/wheels/get", **BASE_SCHEMA})
@callback
def websocket_get_wheel_preferences(hass, connection, msg):
    if not _authorize(hass, connection, msg):
        return
    connection.send_result(msg["id"], read_wheel_preferences(_runtime(hass), msg.get("profile_id"), connection.user.id))


@websocket_api.websocket_command({vol.Required("type"): "maverick_music_flow/wheels/set", **BASE_SCHEMA,
    vol.Required("scope"): vol.In(["user", "global"]), vol.Required("context"): str,
    vol.Required("preference"): dict})
@websocket_api.async_response
async def websocket_set_wheel_preferences(hass, connection, msg):
    if not _authorize(hass, connection, msg):
        return
    try:
        result = await save_wheel_preferences(_runtime(hass), _command_payload(msg), connection.user.id, connection.user.is_admin)
        connection.send_result(msg["id"], result)
    except Exception as err:
        connection.send_error(msg["id"], "wheel_save_failed", str(err))


@websocket_api.websocket_command({vol.Required("type"): "maverick_music_flow/playlists", **BASE_SCHEMA,
    vol.Optional("action", default="list"): vol.In(["list", "save", "play", "delete"]),
    vol.Optional("name"): str, vol.Optional("uris"): [str], vol.Optional("playlist_id"): str})
@websocket_api.async_response
async def websocket_saved_playlists(hass, connection, msg):
    if not _authorize(hass, connection, msg):
        return
    try:
        runtime = _runtime(hass)
        payload = _command_payload(msg)
        if msg["action"] == "save": result = await save_playlist(runtime, payload)
        elif msg["action"] == "play": result = await play_playlist(runtime, payload)
        elif msg["action"] == "delete": result = await delete_playlist(runtime, payload)
        else: result = list_playlists(runtime, msg.get("profile_id") or "default")
        connection.send_result(msg["id"], result)
    except Exception as err:
        connection.send_error(msg["id"], "saved_playlist_failed", str(err))
