"""Message and partial-result limits of the Music Assistant WebSocket client.

The real ``MusicAssistantEventClient`` talks to a small aiohttp WebSocket server that plays
the Music Assistant side. Only the ``.const`` import is replaced, so Home Assistant is not
needed. The tests are skipped where aiohttp is not installed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase, main, skipUnless

try:
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestServer
except ImportError:  # pragma: no cover - aiohttp is part of the test requirements
    aiohttp = None

try:
    import pytest

    # Home Assistant's pytest plugin blocks sockets; these tests use a local server.
    pytestmark = pytest.mark.usefixtures("socket_enabled")
except ImportError:  # plain unittest run
    pass

SOURCE = Path(__file__).resolve().parents[1] / "custom_components/maverick_music_flow/ma_client.py"
KIB = 1024
MIB = 1024 * KIB


def load_client_module() -> dict:
    text = SOURCE.read_text(encoding="utf-8").replace(
        "from .const import MUSIC_ASSISTANT_SCHEMA_MIN, MUSIC_ASSISTANT_SCHEMA_VALIDATED",
        "MUSIC_ASSISTANT_SCHEMA_MIN = 1\nMUSIC_ASSISTANT_SCHEMA_VALIDATED = 1",
    )
    namespace: dict = {"__name__": "ma_client_under_test"}
    exec(compile(text, str(SOURCE), "exec"), namespace)
    return namespace


class LimitConstantsTests(TestCase):
    def test_websocket_size_is_bounded_and_not_unlimited(self):
        text = SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("max_msg_size=0", text)
        self.assertIn("max_msg_size=MAX_MESSAGE_SIZE", text)

    @skipUnless(aiohttp, "aiohttp is not installed")
    def test_default_limits(self):
        ns = load_client_module()
        self.assertEqual(ns["MAX_MESSAGE_SIZE"], 64 * MIB)
        self.assertEqual(ns["MAX_PARTIAL_BYTES"], 64 * MIB)
        self.assertGreater(ns["MAX_PARTIAL_ITEMS"], 500)


class FakeMusicAssistant:
    """Answers ``auth`` and a few test commands; ``big`` replies with an oversized message."""

    def __init__(self) -> None:
        self.connections = 0
        self.close_codes: list[int | None] = []

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connections += 1
        await ws.send_json({"server_version": "2.10.0", "schema_version": 1, "server_id": "fake"})
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            payload = json.loads(message.data)
            message_id, command = payload["message_id"], payload["command"]
            args = payload.get("args") or {}
            if command == "auth":
                await ws.send_json({"message_id": message_id, "result": {"authenticated": True}})
            elif command == "ok":
                await ws.send_json({"message_id": message_id, "result": {"ok": True}})
            elif command == "big":
                await ws.send_str(
                    json.dumps({"message_id": message_id, "result": "x" * args["size"]})
                )
            elif command == "chunks":
                for _ in range(args["count"]):
                    await ws.send_json(
                        {
                            "message_id": message_id,
                            "result": ["x" * args["item_size"]] * args["per_chunk"],
                            "partial": True,
                        }
                    )
                await ws.send_json({"message_id": message_id, "result": ["last"]})
        self.close_codes.append(ws.close_code)
        return ws


@skipUnless(aiohttp, "aiohttp is not installed")
class WebSocketLimitTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.ns = load_client_module()
        self.ns["MAX_MESSAGE_SIZE"] = 256 * KIB
        self.fake = FakeMusicAssistant()
        app = web.Application()
        app.router.add_get("/ws", self.fake.handle)
        self.server = TestServer(app)
        await self.server.start_server()
        self.session = aiohttp.ClientSession()
        self.events: list[dict] = []
        self.client = self.ns["MusicAssistantEventClient"](self.session, self.events.append)
        self.client.configure(f"http://127.0.0.1:{self.server.port}", "token")
        await self.wait_authenticated()

    async def asyncTearDown(self) -> None:
        await self.client.async_stop()
        await self.session.close()
        await self.server.close()

    async def wait_authenticated(self, timeout: float = 8) -> None:
        async with asyncio.timeout(timeout):
            while not self.client._authenticated:
                await asyncio.sleep(0.01)

    async def test_oversized_message_closes_the_socket_and_fails_pending_commands(self):
        self.assertEqual(self.fake.connections, 1)
        started = asyncio.get_running_loop().time()
        with self.assertRaisesRegex(RuntimeError, "disconnected: .*larger than"):
            await self.client.async_command("big", {"size": 1 * MIB}, timeout=10)
        # The command failed because the socket closed, not because it timed out.
        self.assertLess(asyncio.get_running_loop().time() - started, 5)
        self.assertEqual(self.client._oversized_messages, 1)
        self.assertIn("larger than", self.client._last_error)
        self.assertFalse(self.client._pending_commands)
        self.assertFalse(self.client._partial_results)
        self.assertEqual(self.client.snapshot()["limits"]["oversized_messages"], 1)
        # aiohttp closes with 1009 (message too big), which the server side observes.
        async with asyncio.timeout(5):
            while not self.fake.close_codes:
                await asyncio.sleep(0.01)
        self.assertEqual(self.fake.close_codes[0], 1009)

    async def test_client_reconnects_after_an_oversized_message(self):
        with self.assertRaises(RuntimeError):
            await self.client.async_command("big", {"size": 1 * MIB}, timeout=10)
        await self.wait_authenticated()
        self.assertEqual(self.fake.connections, 2)
        self.assertEqual(await self.client.async_command("ok"), {"ok": True})

    async def test_message_under_the_limit_is_delivered(self):
        result = await self.client.async_command("big", {"size": 64 * KIB})
        self.assertEqual(len(result), 64 * KIB)
        self.assertEqual(self.fake.connections, 1)

    async def test_partial_results_over_the_item_limit_fail_only_that_command(self):
        self.ns["MAX_PARTIAL_ITEMS"] = 10
        with self.assertRaisesRegex(RuntimeError, "too large"):
            await self.client.async_command(
                "chunks", {"count": 3, "per_chunk": 5, "item_size": 1}, timeout=10
            )
        self.assertEqual(self.client._partial_limit_failures, 1)
        self.assertFalse(self.client._pending_commands)
        self.assertFalse(self.client._partial_results)
        self.assertFalse(self.client._partial_sizes)
        # The connection is still up and serves the next command.
        self.assertEqual(self.fake.connections, 1)
        self.assertEqual(await self.client.async_command("ok"), {"ok": True})

    async def test_partial_results_over_the_byte_limit_fail_the_command(self):
        self.ns["MAX_PARTIAL_BYTES"] = 5 * KIB
        with self.assertRaisesRegex(RuntimeError, "too large"):
            await self.client.async_command(
                "chunks", {"count": 4, "per_chunk": 1, "item_size": 2 * KIB}, timeout=10
            )
        self.assertEqual(self.client._partial_limit_failures, 1)
        self.assertFalse(self.client._partial_results)
        self.assertEqual(await self.client.async_command("ok"), {"ok": True})

    async def test_final_chunk_counts_toward_the_limit(self):
        self.ns["MAX_PARTIAL_ITEMS"] = 10
        # 2 chunks of 5 items fit exactly; the final one-item message pushes it over.
        with self.assertRaisesRegex(RuntimeError, "too large"):
            await self.client.async_command(
                "chunks", {"count": 2, "per_chunk": 5, "item_size": 1}, timeout=10
            )

    async def test_partial_results_within_limits_are_joined(self):
        result = await self.client.async_command(
            "chunks", {"count": 3, "per_chunk": 4, "item_size": 10}, timeout=10
        )
        self.assertEqual(len(result), 13)
        self.assertEqual(result[-1], "last")
        self.assertFalse(self.client._partial_results)
        self.assertFalse(self.client._partial_sizes)


if __name__ == "__main__":
    main()
