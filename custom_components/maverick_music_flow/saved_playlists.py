"""Small Engine-owned playlists; playback uses Music Assistant's queue API."""

from __future__ import annotations

import copy
import uuid

from .storage_limits import (
    MAX_PLAYLIST_BYTES,
    MAX_PLAYLISTS_PER_PROFILE,
    MAX_URI_LENGTH,
    MAX_URIS_PER_PLAYLIST,
    check_profile,
    json_size,
)


def list_playlists(runtime, profile="default"):
    return copy.deepcopy(
        list(runtime._storage.get("saved_playlists", {}).get(profile, {}).values())
    )


async def save_playlist(runtime, payload, is_admin=False):
    profile = str(payload.get("profile_id") or "default")
    name = str(payload.get("name") or "").strip()
    uris = payload.get("uris")
    if not name or len(name) > 120:
        raise ValueError("A playlist name of 1-120 characters is required")
    if not isinstance(uris, list) or not 1 <= len(uris) <= MAX_URIS_PER_PLAYLIST:
        raise ValueError(f"A playlist needs 1-{MAX_URIS_PER_PLAYLIST} media URIs")
    if any(
        not isinstance(uri, str)
        or "://" not in uri
        or len(uri) > MAX_URI_LENGTH
        or any(ord(c) < 32 for c in uri)
        for uri in uris
    ):
        raise ValueError(f"Invalid media URI (each must be at most {MAX_URI_LENGTH} characters)")
    if json_size(uris) > MAX_PLAYLIST_BYTES:
        raise ValueError(f"Playlist is too large (limit {MAX_PLAYLIST_BYTES // 1024} KiB)")
    check_profile(runtime._storage, profile, is_admin)
    existing = runtime._storage.get("saved_playlists", {}).get(profile, {})
    if not payload.get("playlist_id") and len(existing) >= MAX_PLAYLISTS_PER_PROFILE:
        raise ValueError(f"Playlist limit reached ({MAX_PLAYLISTS_PER_PROFILE} per profile)")
    store = runtime._storage.setdefault("saved_playlists", {}).setdefault(profile, {})
    playlist_id = str(payload.get("playlist_id") or uuid.uuid4().hex)
    if payload.get("playlist_id") and playlist_id not in store:
        raise ValueError("Playlist no longer exists")
    previous = copy.deepcopy(store.get(playlist_id))
    item = {"id": playlist_id, "name": name, "uris": list(uris)}
    store[playlist_id] = item
    try:
        await runtime.async_save()
    except Exception:
        if previous is None:
            store.pop(playlist_id, None)
            if not store:
                runtime._storage["saved_playlists"].pop(profile, None)
        else:
            store[playlist_id] = previous
        raise
    return copy.deepcopy(item)


async def play_playlist(runtime, payload):
    profile = str(payload.get("profile_id") or "default")
    item = (
        runtime._storage.get("saved_playlists", {}).get(profile, {}).get(payload.get("playlist_id"))
    )
    player = payload.get("selected_player")
    if not item or not player:
        raise ValueError("Choose a saved playlist and an available player")
    if not runtime._player_readiness(player).get("ready"):
        raise ValueError("The selected player is unavailable")
    ma_player = runtime._resolve_ma_player_id(player)
    result = await runtime.async_music_assistant_command(
        {"command": "player_queues/get_active_queue", "args": {"player_id": ma_player}}
    )
    queue = result.get("data", result) if isinstance(result, dict) else result
    if not isinstance(queue, dict) or not queue.get("queue_id"):
        raise ValueError("No active queue for the selected player")
    return await runtime.async_music_assistant_command(
        {
            "command": "player_queues/play_media",
            "args": {
                "queue_id": queue["queue_id"],
                "media": list(item["uris"]),
                "option": "replace",
            },
        }
    )


async def delete_playlist(runtime, payload):
    profile = str(payload.get("profile_id") or "default")
    store = runtime._storage.get("saved_playlists", {}).get(profile, {})
    key = payload.get("playlist_id")
    previous = store.pop(key, None)
    if previous is None:
        return {"deleted": False}
    try:
        await runtime.async_save()
    except Exception:
        store[key] = previous
        raise
    return {"deleted": True}
