"""URL media validation (S-5): callers cannot make Music Assistant fetch local addresses.

The policy module runs as is; the runtime methods that use it are extracted from
runtime.py and run against fake HA/MA boundaries, without installing Home Assistant.
DNS is replaced by a fake resolver so no test touches the network.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import json
import runpy
import time
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import IsolatedAsyncioTestCase, TestCase, main
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/maverick_music_flow"
POLICY = runpy.run_path(str(COMPONENT / "media_url_policy.py"))
MediaUrlNotAllowed = POLICY["MediaUrlNotAllowed"]
validate_url = POLICY["async_validate_media_url"]
validate_reference = POLICY["async_validate_media_reference"]
embedded_media_urls = POLICY["embedded_media_urls"]
command_media_references = POLICY["command_media_references"]

DNS = {
    "radio.example": ["93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"],
    "nas.example": ["192.168.1.20"],
    "internal.example": ["10.0.0.5"],
    "rebind.example": ["93.184.215.14", "127.0.0.1"],
    "metadata.example": ["169.254.169.254"],
}
PUBLIC_URL = "https://radio.example/stream.mp3"
LOCAL_URLS = (
    "http://127.0.0.1/",
    "http://192.168.1.1/",
    "http://[::1]/",
    "http://169.254.169.254/latest/meta-data/",
)


async def fake_resolve(host: str, _port: int) -> list[str]:
    if host in DNS:
        return DNS[host]
    raise OSError("unknown host")


async def check(url: str, **kwargs: Any) -> None:
    await validate_url(url, resolve=fake_resolve, **kwargs)


class ValidateUrlTests(IsolatedAsyncioTestCase):
    async def test_public_https_url_is_accepted(self):
        await check(PUBLIC_URL)
        await check("http://93.184.215.14/chime.mp3")
        await check("HTTPS://RADIO.EXAMPLE:8443/live")

    async def test_local_addresses_are_rejected(self):
        for url in (
            *LOCAL_URLS,
            "http://localhost:8123/",
            "http://api.localhost/",
            "http://0.0.0.0/",
            "http://10.1.2.3/",
            "http://172.16.0.1/",
            "http://100.64.0.1/",
            "http://[fd00::1]/",
            "http://[fe80::1%25eth0]/",
            "http://[::ffff:127.0.0.1]/",
            "http://[::ffff:192.168.1.1]/",
            "http://224.0.0.1/",
            "http://240.0.0.1/",
            "http://internal.example/",
            "http://metadata.example/",
        ):
            with self.subTest(url=url), self.assertRaises(MediaUrlNotAllowed):
                await check(url)

    async def test_host_with_any_private_address_is_rejected(self):
        with self.assertRaises(MediaUrlNotAllowed):
            await check("http://rebind.example/")

    async def test_unresolvable_host_is_rejected(self):
        with self.assertRaisesRegex(MediaUrlNotAllowed, "could not be resolved"):
            await check("http://missing.example/")

    async def test_only_http_and_https_are_accepted(self):
        for url in ("ftp://radio.example/a.mp3", "file:///etc/passwd", "rtsp://radio.example/x"):
            with (
                self.subTest(url=url),
                self.assertRaisesRegex(MediaUrlNotAllowed, "only http and https"),
            ):
                await check(url)

    async def test_ambiguous_urls_are_rejected(self):
        for url in (
            "http://user:pass@radio.example/",
            "http://radio.example@127.0.0.1/",
            "http://radio.example\\@127.0.0.1/",
            "http://radio.example /x",
            "http://radio.example\t/x",
            "http://radio.example:99999/",
            "http:///path",
        ):
            with self.subTest(url=url), self.assertRaises(MediaUrlNotAllowed):
                await check(url)

    async def test_errors_never_echo_host_or_address(self):
        for url, secret in (
            ("http://internal.example/a?token=abc", "10.0.0.5"),
            ("http://192.168.1.1/a?token=abc", "192.168.1.1"),
            ("http://missing.example/a?token=abc", "missing.example"),
        ):
            with self.subTest(url=url):
                with self.assertRaises(MediaUrlNotAllowed) as caught:
                    await check(url)
                message = str(caught.exception)
                self.assertNotIn(secret, message)
                self.assertNotIn("internal.example", message)
                self.assertNotIn("token", message)

    async def test_configured_music_assistant_and_home_assistant_origins_are_trusted(self):
        bases = ["http://192.168.1.10:8095", "http://homeassistant.local:8123"]
        await check("http://192.168.1.10:8095/announce.mp3", trusted_bases=bases)
        await check("http://homeassistant.local:8123/local/chime.mp3", trusted_bases=bases)
        await check("http://HomeAssistant.local.:8123/local/chime.mp3", trusted_bases=bases)

    async def test_trust_is_limited_to_the_exact_origin(self):
        bases = ["http://192.168.1.10:8095", "http://127.0.0.1:8123"]
        for url in (
            "http://192.168.1.10:22/",
            "https://192.168.1.10:8095/",
            "http://127.0.0.1:8124/",
            "http://192.168.1.11:8095/",
        ):
            with self.subTest(url=url), self.assertRaises(MediaUrlNotAllowed):
                await check(url, trusted_bases=bases)

    async def test_local_network_option_allows_lan_but_not_loopback_or_link_local(self):
        for url in (
            "http://192.168.1.1/",
            "http://10.1.2.3/",
            "http://172.16.0.1/",
            "http://100.64.0.1/",
            "http://[fd00::1]/",
            "http://nas.example/a.mp3",
            "http://[::ffff:192.168.1.1]/",
        ):
            with self.subTest(url=url):
                await check(url, allow_local=True)
        for url in (
            "http://127.0.0.1/",
            "http://[::1]/",
            "http://169.254.169.254/",
            "http://metadata.example/",
            "http://localhost/",
            "http://0.0.0.0/",
            "http://224.0.0.1/",
        ):
            with self.subTest(url=url), self.assertRaises(MediaUrlNotAllowed):
                await check(url, allow_local=True)

    async def test_refusal_mentions_the_option_only_when_it_is_off(self):
        with self.assertRaisesRegex(MediaUrlNotAllowed, "options"):
            await check("http://192.168.1.1/")
        with self.assertRaises(MediaUrlNotAllowed) as caught:
            await check("http://127.0.0.1/", allow_local=True)
        self.assertNotIn("options", str(caught.exception))

    async def test_default_resolver_is_used_for_literal_addresses_without_lookup(self):
        resolver = AsyncMock()
        with self.assertRaises(MediaUrlNotAllowed):
            await validate_url("http://127.0.0.1/", resolve=resolver)
        resolver.assert_not_awaited()


class MediaReferenceTests(IsolatedAsyncioTestCase):
    async def test_music_assistant_uris_are_untouched(self):
        resolver = AsyncMock()
        for uri in (
            "library://track/42",
            "spotify://track/4uLU6hMCjMI75M1A2tKUQC",
            "spotify:track:4uLU6hMCjMI75M1A2tKUQC",
            "radiobrowser://radio/9617a958-0601-11e8-ae97-52543be04c81",
            "tidal://album/123",
            "apple_music://playlist/pl.abc",
            "42",
            "media-source://tts/test",
        ):
            with self.subTest(uri=uri):
                self.assertEqual(embedded_media_urls(uri), [])
                await validate_reference(uri, resolve=resolver)
        resolver.assert_not_awaited()

    async def test_urls_embedded_in_music_assistant_uris_are_validated(self):
        for uri in (
            "builtin://track/http://127.0.0.1:8123/api",
            "builtin://radio/https%3A%2F%2F192.168.1.1%2Fstream",
            "builtin://track/http%253A%252F%252F169.254.169.254%252F",
            "podcastfeed://podcast/HTTP://[::1]/feed.xml",
        ):
            with self.subTest(uri=uri), self.assertRaises(MediaUrlNotAllowed):
                await validate_reference(uri, resolve=fake_resolve)
        await validate_reference(f"builtin://track/{PUBLIC_URL}", resolve=fake_resolve)

    async def test_plain_urls_are_validated(self):
        await validate_reference(PUBLIC_URL, resolve=fake_resolve)
        for url in LOCAL_URLS:
            with self.subTest(url=url), self.assertRaises(MediaUrlNotAllowed):
                await validate_reference(url, resolve=fake_resolve)

    def test_command_media_references(self):
        self.assertEqual(
            command_media_references(
                "player_queues/play_media",
                {"queue_id": "q", "media": ["library://track/1", {"uri": "http://127.0.0.1/"}, 7]},
            ),
            ["library://track/1", "http://127.0.0.1/"],
        )
        self.assertEqual(command_media_references("music/item_by_uri", {"uri": "x"}), ["x"])
        self.assertEqual(
            command_media_references("music/library/add_item", {"item": {"uri": "y"}}), ["y"]
        )
        self.assertEqual(command_media_references("music/favorites/add_item", {"item": "z"}), ["z"])
        self.assertEqual(
            command_media_references(
                "music/playlists/add_playlist_tracks", {"db_playlist_id": 1, "uris": ["a", "b"]}
            ),
            ["a", "b"],
        )
        self.assertEqual(
            command_media_references("music/search", {"search_query": "http://127.0.0.1/"}), []
        )
        self.assertEqual(command_media_references("player_queues/play_media", None), [])

    def test_every_media_command_is_on_the_bridge_allowlist(self):
        bridge = (COMPONENT / "command_bridge.py").read_text(encoding="utf-8")
        for command in POLICY["MEDIA_REFERENCE_ARGS"]:
            with self.subTest(command=command):
                self.assertIn(f'"{command}",', bridge)


def load_runtime():
    """Extract the runtime methods that pass media to Music Assistant."""
    source = COMPONENT / "runtime.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HomeiiFlowRuntime"
    )
    cls.decorator_list = []
    wanted = {
        "_async_validate_media_reference",
        "local_media_urls_allowed",
        "async_send_announcement",
        "async_play_media",
        "async_music_assistant_command",
        "_music_assistant_command_cacheable",
        "_music_assistant_command_cache_key",
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
        "tts": SimpleNamespace(
            generate_media_source_id=Mock(return_value="media-source://tts/test")
        ),
        "music_assistant_command_allowed": lambda command: True,
        "command_media_references": command_media_references,
        "async_validate_media_reference": (
            lambda reference, **kwargs: validate_reference(
                reference, resolve=fake_resolve, **kwargs
            )
        ),
        "home_assistant_base_url": lambda hass: hass.ha_url,
        "HomeiiFlowServiceUnavailable": RuntimeError,
        "_clean_string": lambda value: str(value or "").strip(),
        "_dict_first": lambda d, *keys: next((d[k] for k in keys if d.get(k)), None),
        "_safe_list": lambda value: value if isinstance(value, list) else [],
        "_utc_iso": lambda: "now",
        "DEFAULT_PROFILE_ID": "default",
    }
    tree = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            cls,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(tree), str(source), "exec"), ns)
    return ns["HomeiiFlowRuntime"]


Runtime = load_runtime()


class RuntimeTests(IsolatedAsyncioTestCase):
    def runtime(self, *, allow_local: bool = False):
        runtime = Runtime()
        runtime.hass = SimpleNamespace(
            ha_url="http://homeassistant.local:8123",
            services=SimpleNamespace(has_service=lambda *args: True),
            async_create_task=lambda coro: asyncio.get_running_loop().create_task(coro),
        )
        entries = {"kitchen": SimpleNamespace(allow_local_media_urls=allow_local)}
        runtime._matching_entry = lambda instance_id=None: entries.get(instance_id or "kitchen")
        runtime.music_assistant_base_urls = lambda: ["http://192.168.1.10:8095"]
        runtime._announcement_targets = lambda payload: payload["players"]
        runtime._preferred_announcement_say_service = lambda: "google_translate_say"
        runtime.async_record_announcement = AsyncMock(return_value={"announcement": {}})
        runtime.async_record_activity = AsyncMock()
        runtime.async_call_service_response = AsyncMock()
        runtime._player_readiness = lambda player: {"ready": True}
        runtime._resolve_ma_player_id = lambda player: player
        runtime._bump_snapshot_revision = Mock()
        runtime._music_assistant_client = SimpleNamespace(
            snapshot=lambda: {"authenticated": True, "schema_supported": True, "connected": True},
            async_command=AsyncMock(return_value={"ok": True}),
        )
        runtime._ma_http_health = {}
        runtime._media_cache_metrics = defaultdict(int)
        runtime._media_command_cache = {}
        runtime._media_command_inflight = {}
        runtime.decorate_artwork_urls = lambda value: value
        runtime._schedule_media_cache_save = Mock()
        runtime._mark_library_cache_stale = Mock()
        runtime._mark_media_command_cache_stale = Mock()
        return runtime

    async def test_announcement_with_local_url_never_reaches_music_assistant(self):
        for url in LOCAL_URLS:
            runtime = self.runtime()
            with self.subTest(url=url), self.assertRaises(MediaUrlNotAllowed):
                await runtime.async_send_announcement(
                    {"message": url, "players": ["media_player.kitchen"]}
                )
            runtime.async_call_service_response.assert_not_awaited()
            runtime.async_record_announcement.assert_not_awaited()

    async def test_announcement_with_public_url_is_played(self):
        runtime = self.runtime()
        await runtime.async_send_announcement(
            {"message": PUBLIC_URL, "players": ["media_player.kitchen"]}
        )
        call = runtime.async_call_service_response.await_args
        self.assertEqual(call.args[:2], ("music_assistant", "play_announcement"))
        self.assertEqual(call.args[2]["url"], PUBLIC_URL)

    async def test_announcement_from_home_assistant_url_is_played(self):
        runtime = self.runtime()
        url = "http://homeassistant.local:8123/local/doorbell.mp3"
        await runtime.async_send_announcement({"message": url, "players": ["media_player.kitchen"]})
        self.assertEqual(runtime.async_call_service_response.await_args.args[2]["url"], url)

    async def test_text_announcement_is_not_treated_as_url(self):
        runtime = self.runtime()
        await runtime.async_send_announcement(
            {"message": "Dinner at 127.0.0.1 o'clock", "players": ["a"]}
        )
        self.assertEqual(
            runtime.async_call_service_response.await_args.args[:2], ("tts", "google_translate_say")
        )

    async def test_local_network_option_allows_lan_announcement(self):
        runtime = self.runtime(allow_local=True)
        await runtime.async_send_announcement(
            {"message": "http://192.168.1.1/chime.mp3", "players": ["a"], "instance_id": "kitchen"}
        )
        runtime.async_call_service_response.assert_awaited_once()
        runtime = self.runtime(allow_local=True)
        with self.assertRaises(MediaUrlNotAllowed):
            await runtime.async_send_announcement(
                {"message": "http://169.254.169.254/", "players": ["a"]}
            )

    async def test_play_media_with_local_url_never_reaches_music_assistant(self):
        for url in LOCAL_URLS:
            runtime = self.runtime()
            runtime.async_music_assistant_command = AsyncMock()
            with self.subTest(url=url), self.assertRaises(MediaUrlNotAllowed):
                await runtime.async_play_media({"player": "media_player.kitchen", "media_id": url})
            runtime.async_music_assistant_command.assert_not_awaited()

    async def test_play_media_passes_public_urls_and_provider_uris_unchanged(self):
        for media_id in (PUBLIC_URL, "library://track/42", "spotify://track/abc"):
            runtime = self.runtime()
            runtime.async_music_assistant_command = AsyncMock(
                return_value={"data": {"queue_id": "q"}}
            )
            with self.subTest(media_id=media_id):
                result = await runtime.async_play_media(
                    {"player": "media_player.kitchen", "media_id": media_id}
                )
                play = runtime.async_music_assistant_command.await_args_list[1].args[0]
                self.assertEqual(play["command"], "player_queues/play_media")
                self.assertEqual(play["args"]["media"], media_id)
                self.assertEqual(result["media_id"], media_id)

    async def test_bridge_refuses_local_urls_in_media_commands(self):
        for command, args in (
            ("player_queues/play_media", {"queue_id": "q", "media": "http://127.0.0.1/"}),
            (
                "player_queues/play_media",
                {"queue_id": "q", "media": ["library://track/1", "http://[::1]/"]},
            ),
            (
                "player_queues/play_media",
                {"queue_id": "q", "media": {"uri": "builtin://track/http://192.168.1.1/"}},
            ),
            ("music/item_by_uri", {"uri": "http://169.254.169.254/"}),
            ("music/library/add_item", {"item": "http://192.168.1.1/a.mp3"}),
            ("music/favorites/add_item", {"item": {"uri": "http://127.0.0.1/"}}),
            (
                "music/playlists/add_playlist_tracks",
                {"db_playlist_id": "1", "uris": ["http://10.0.0.1/a.mp3"]},
            ),
        ):
            runtime = self.runtime()
            with self.subTest(command=command, args=args), self.assertRaises(MediaUrlNotAllowed):
                await runtime.async_music_assistant_command({"command": command, "args": args})
            runtime._music_assistant_client.async_command.assert_not_awaited()

    async def test_bridge_passes_provider_uris_and_public_urls(self):
        runtime = self.runtime()
        args = {
            "queue_id": "q",
            "media": ["library://track/1", "spotify://track/abc", PUBLIC_URL],
            "option": "replace",
        }
        await runtime.async_music_assistant_command(
            {"command": "player_queues/play_media", "args": args}
        )
        runtime._music_assistant_client.async_command.assert_awaited_once()
        self.assertEqual(runtime._music_assistant_client.async_command.await_args.args[1], args)


class OptionWiringTests(TestCase):
    def test_option_is_registered_and_documented(self):
        const = (COMPONENT / "const.py").read_text(encoding="utf-8")
        self.assertIn('CONF_ALLOW_LOCAL_MEDIA_URLS = "allow_local_media_urls"', const)
        init = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("entry.options.get(CONF_ALLOW_LOCAL_MEDIA_URLS, False)", init)
        self.assertIn("allow_local_media_urls=allow_local_media_urls", init)
        # The announce and play_media actions report a refused URL as a validation error.
        self.assertEqual(init.count("except MediaUrlNotAllowed as error:"), 2)
        flow = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")
        self.assertIn("updated[CONF_ALLOW_LOCAL_MEDIA_URLS] = bool(", flow)
        self.assertIn("self._config_entry.options.get(CONF_ALLOW_LOCAL_MEDIA_URLS, False)", flow)
        for name in ("strings.json", "translations/en.json"):
            general = json.loads((COMPONENT / name).read_text(encoding="utf-8"))["options"]["step"][
                "general"
            ]
            with self.subTest(file=name):
                self.assertEqual(
                    general["data"]["allow_local_media_urls"],
                    "Allow announcements and playback from local network URLs",
                )
                self.assertIn(
                    "Off by default", general["data_description"]["allow_local_media_urls"]
                )
        self.assertIn(
            "Allow announcements and playback from local network URLs",
            (ROOT / "README.md").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    main()
