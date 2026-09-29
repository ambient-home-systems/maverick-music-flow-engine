import ast
import copy
import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

source = (
    Path(__file__).resolve().parents[1] / "custom_components/maverick_music_flow/saved_playlists.py"
)

_limits_src = (
    Path(__file__).resolve().parents[1] / "custom_components/maverick_music_flow/storage_limits.py"
)
_limits_ns = {"__name__": "storage_limits", "json": json, "re": re}
exec(compile(_limits_src.read_text(encoding="utf-8"), str(_limits_src), "exec"), _limits_ns)
ns = {"copy": copy, "uuid": uuid, **{k: v for k, v in _limits_ns.items() if not k.startswith("__")}}
tree = ast.parse(source.read_text(encoding="utf-8"))
exec(
    compile(
        ast.Module(
            body=[n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))],
            type_ignores=[],
        ),
        str(source),
        "exec",
    ),
    ns,
)


class SavedPlaylistTests(IsolatedAsyncioTestCase):
    async def test_save_profile_and_rollback(self):
        runtime = SimpleNamespace(_storage={}, async_save=AsyncMock())
        item = await ns["save_playlist"](
            runtime, {"name": "Evening", "uris": ["spotify://track/one"]}
        )
        self.assertEqual(ns["list_playlists"](runtime), [item])
        self.assertEqual(ns["list_playlists"](runtime, "other"), [])
        runtime.async_save.side_effect = RuntimeError("disk")
        with self.assertRaises(RuntimeError):
            await ns["save_playlist"](
                runtime,
                {"name": "Changed", "uris": ["spotify://track/two"], "playlist_id": item["id"]},
            )
        self.assertEqual(ns["list_playlists"](runtime), [item])

    async def test_play_uses_active_queue_and_one_atomic_media_list(self):
        runtime = SimpleNamespace(
            _storage={},
            async_save=AsyncMock(),
            _player_readiness=lambda _: {"ready": True},
            _resolve_ma_player_id=lambda _: "native",
            async_music_assistant_command=AsyncMock(
                side_effect=[{"data": {"queue_id": "group"}}, {"ok": True}]
            ),
        )
        item = await ns["save_playlist"](
            runtime, {"name": "Mix", "uris": ["spotify://track/one", "spotify://track/two"]}
        )
        await ns["play_playlist"](
            runtime, {"playlist_id": item["id"], "selected_player": "media_player.computer"}
        )
        runtime.async_music_assistant_command.assert_awaited_with(
            {
                "command": "player_queues/play_media",
                "args": {"queue_id": "group", "media": item["uris"], "option": "replace"},
            }
        )

    async def test_delete_is_profile_scoped_and_rolls_back_failed_persistence(self):
        runtime = SimpleNamespace(_storage={}, async_save=AsyncMock())
        item = await ns["save_playlist"](runtime, {"name": "Mix", "uris": ["library://track/one"]})
        self.assertEqual(
            await ns["delete_playlist"](
                runtime, {"profile_id": "other", "playlist_id": item["id"]}
            ),
            {"deleted": False},
        )
        runtime.async_save.side_effect = RuntimeError("disk")
        with self.assertRaises(RuntimeError):
            await ns["delete_playlist"](runtime, {"playlist_id": item["id"]})
        self.assertEqual(ns["list_playlists"](runtime), [item])
        runtime.async_save.side_effect = None
        self.assertEqual(
            await ns["delete_playlist"](runtime, {"playlist_id": item["id"]}), {"deleted": True}
        )
        self.assertEqual(ns["list_playlists"](runtime), [])


class SavedPlaylistLimitTests(IsolatedAsyncioTestCase):
    def runtime(self):
        return SimpleNamespace(_storage={}, async_save=AsyncMock())

    async def save(self, runtime, uris=None, is_admin=False, **extra):
        return await ns["save_playlist"](
            runtime,
            {"name": "P", "uris": uris or ["spotify://track/one"], **extra},
            is_admin,
        )

    async def test_uri_count_length_and_total_size_limits(self):
        runtime = self.runtime()
        await self.save(runtime, ["a://" + "x"] * ns["MAX_URIS_PER_PLAYLIST"])
        with self.assertRaisesRegex(ValueError, "media URIs"):
            await self.save(runtime, ["a://x"] * (ns["MAX_URIS_PER_PLAYLIST"] + 1))
        await self.save(runtime, ["a://" + "x" * (ns["MAX_URI_LENGTH"] - 4)])
        with self.assertRaisesRegex(ValueError, "Invalid media URI"):
            await self.save(runtime, ["a://" + "x" * ns["MAX_URI_LENGTH"]])
        ns_small = dict(ns, MAX_PLAYLIST_BYTES=100)
        fn = ns_small["save_playlist"]
        fn.__globals__["MAX_PLAYLIST_BYTES"] = 100
        try:
            with self.assertRaisesRegex(ValueError, "too large"):
                await self.save(runtime, ["a://" + "x" * 200])
        finally:
            fn.__globals__["MAX_PLAYLIST_BYTES"] = ns_small["MAX_PLAYLIST_BYTES"] = 256 * 1024

    async def test_playlist_count_limit_per_profile(self):
        runtime = self.runtime()
        for _ in range(ns["MAX_PLAYLISTS_PER_PROFILE"]):
            await self.save(runtime)
        with self.assertRaisesRegex(ValueError, "Playlist limit"):
            await self.save(runtime)
        existing = ns["list_playlists"](runtime)[0]["id"]
        await self.save(runtime, playlist_id=existing)  # editing is still allowed
        await self.save(runtime, is_admin=True, profile_id="other")  # other profile has room

    async def test_invalid_profile_ids_rejected(self):
        runtime = self.runtime()
        for bad in ["UPPER", "has space", "a" * 33, "../x", "é", "a.b"]:
            with self.assertRaisesRegex(ValueError, "Invalid profile ID"):
                await self.save(runtime, is_admin=True, profile_id=bad)
        self.assertEqual(runtime._storage, {})

    async def test_only_admin_creates_profiles_and_count_is_capped(self):
        runtime = self.runtime()
        with self.assertRaisesRegex(ValueError, "administrator"):
            await self.save(runtime, profile_id="kitchen")
        await self.save(runtime, is_admin=True, profile_id="kitchen")
        await self.save(runtime, profile_id="kitchen")  # existing: non-admin may save
        await self.save(runtime)  # default profile needs no admin
        for i in range(ns["MAX_PROFILES"] - 1):
            await self.save(runtime, is_admin=True, profile_id=f"p{i}")
        with self.assertRaisesRegex(ValueError, "Profile limit"):
            await self.save(runtime, is_admin=True, profile_id="one-too-many")

    async def test_failed_first_save_does_not_leave_empty_profile(self):
        runtime = self.runtime()
        runtime.async_save.side_effect = RuntimeError("disk")
        with self.assertRaises(RuntimeError):
            await self.save(runtime, is_admin=True, profile_id="new")
        self.assertNotIn("new", runtime._storage["saved_playlists"])
