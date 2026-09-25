"""Authenticated HA-to-MA Sendspin transport; MA credentials stay on the server."""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlsplit, urlunsplit

from aiohttp import WSCloseCode, WSMsgType, web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN

# Persisted {client_id: HA user id}. The first user to connect with a client_id owns it.
SENDSPIN_CLIENTS_STORAGE_KEY = "sendspin_clients"
# Oldest client_ids a user owns are released beyond this, so storage cannot grow unbounded.
MAX_CLIENT_IDS_PER_USER = 16
# Concurrent relay sessions; each one holds two WebSockets and streams audio.
MAX_SESSIONS_PER_USER = 6
MAX_SESSIONS_TOTAL = 24


class SendspinIdentityError(Exception):
    """The browser claimed a different Sendspin client_id than the one it was bound to."""


def client_hello_id(data: str) -> tuple[bool, object]:
    """Return (is_client_hello, claimed client_id) for a Sendspin text frame."""
    try:
        message = json.loads(data)
    except ValueError:
        return False, None
    if not isinstance(message, dict) or message.get("type") != "client/hello":
        return False, None
    payload = message.get("payload")
    return True, payload.get("client_id") if isinstance(payload, dict) else None


async def relay_frames(source, destination) -> None:
    """Forward streaming frames with backpressure and stop when either side closes."""
    async for message in source:
        if message.type == WSMsgType.TEXT:
            await destination.send_str(message.data)
        elif message.type == WSMsgType.BINARY:
            await destination.send_bytes(message.data)
        elif message.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED):
            break


async def relay_client_frames(source, destination, client_id: str) -> None:
    """Forward browser frames to MA; MA names the player after the client/hello client_id.

    The first frame must be a client/hello for the bound client_id, and any later hello
    must name it too, so a user cannot register as another user's player.
    """
    hello_seen = False
    async for message in source:
        if message.type == WSMsgType.TEXT:
            is_hello, claimed = client_hello_id(message.data)
            if (is_hello or not hello_seen) and claimed != client_id:
                raise SendspinIdentityError
            hello_seen = True
            await destination.send_str(message.data)
        elif message.type == WSMsgType.BINARY:
            if not hello_seen:
                raise SendspinIdentityError
            await destination.send_bytes(message.data)
        elif message.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED):
            break


class SendspinClientRegistry:
    """Bind each Sendspin client_id to one HA user and cap concurrent relay sessions."""

    def __init__(self, hass):
        self.hass = hass
        self._sessions: dict[str, int] = {}
        self._lock = asyncio.Lock()

    def _owners(self, runtime) -> dict:
        owners = runtime._storage.get(SENDSPIN_CLIENTS_STORAGE_KEY)
        if not isinstance(owners, dict):
            owners = runtime._storage[SENDSPIN_CLIENTS_STORAGE_KEY] = {}
        return owners

    async def async_claim(self, runtime, client_id: str, user_id: str) -> bool:
        """Return True when user_id owns client_id, recording ownership on first use."""
        async with self._lock:
            owners = self._owners(runtime)
            owner = owners.get(client_id)
            if owner == user_id:
                owners[client_id] = owners.pop(client_id)  # most recently used last
                return True
            # A client_id whose owner was deleted from HA may be taken over.
            if isinstance(owner, str) and await self.hass.auth.async_get_user(owner) is not None:
                return False
            previous = dict(owners)
            owners.pop(client_id, None)
            owned = [cid for cid, uid in owners.items() if uid == user_id]
            for released in owned[: max(0, len(owned) - MAX_CLIENT_IDS_PER_USER + 1)]:
                owners.pop(released)
            owners[client_id] = user_id
            try:
                await runtime.async_save()
            except Exception:
                owners.clear()
                owners.update(previous)
                raise
            return True

    def acquire_session(self, user_id: str) -> bool:
        """Reserve a relay session slot; False when the user or the Engine is at its cap."""
        if sum(self._sessions.values()) >= MAX_SESSIONS_TOTAL:
            return False
        if self._sessions.get(user_id, 0) >= MAX_SESSIONS_PER_USER:
            return False
        self._sessions[user_id] = self._sessions.get(user_id, 0) + 1
        return True

    def release_session(self, user_id: str) -> None:
        """Free a slot reserved by acquire_session."""
        remaining = self._sessions.get(user_id, 0) - 1
        if remaining > 0:
            self._sessions[user_id] = remaining
        else:
            self._sessions.pop(user_id, None)


class HomeiiFlowSendspinView(HomeAssistantView):
    """Use HA authentication, including its short-lived signed GET paths."""

    url = "/api/maverick_music_flow/sendspin/{client_id}"
    name = "api:maverick_music_flow:sendspin"
    requires_auth = True

    def __init__(self, hass):
        self.hass = hass
        self.registry = SendspinClientRegistry(hass)

    async def get(self, request: web.Request, client_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", client_id):
            raise web.HTTPBadRequest(text="Invalid player id")
        runtime = self.hass.data[DOMAIN]["runtime"]
        urls = runtime.music_assistant_base_urls()
        tokens = runtime.music_assistant_tokens()
        if not urls or not tokens:
            raise web.HTTPServiceUnavailable(text="Configure Music Assistant in HOMEii Flow Engine")
        user_id = request["hass_user"].id
        if not await self.registry.async_claim(runtime, client_id, user_id):
            raise web.HTTPForbidden(text="This player id belongs to another user")
        if not self.registry.acquire_session(user_id):
            raise web.HTTPTooManyRequests(text="Too many local playback connections")
        try:
            return await self._relay(request, client_id, urls[0], tokens[0])
        finally:
            self.registry.release_session(user_id)

    async def _relay(self, request: web.Request, client_id: str, base_url: str, token: str):
        parts = urlsplit(base_url)
        upstream_url = urlunsplit(("wss" if parts.scheme == "https" else "ws", parts.netloc,
                                   parts.path.rstrip("/") + "/sendspin", "", ""))
        session = async_get_clientsession(self.hass)
        upstream = None
        try:
            async with asyncio.timeout(12):
                upstream = await session.ws_connect(upstream_url, heartbeat=30, max_msg_size=4 * 1024 * 1024)
                await upstream.send_json({"type": "auth", "token": token, "client_id": client_id})
                auth = await upstream.receive_json()
                if not isinstance(auth, dict) or auth.get("type") != "auth_ok":
                    raise web.HTTPBadGateway(text="Music Assistant rejected the Sendspin connection")
        except asyncio.CancelledError:
            if upstream is not None:
                await upstream.close()
            raise
        except Exception as error:
            if upstream is not None:
                await upstream.close()
            if isinstance(error, web.HTTPException):
                raise
            raise web.HTTPBadGateway(text="Music Assistant Sendspin is unavailable") from None

        downstream = web.WebSocketResponse(heartbeat=30, max_msg_size=1024 * 1024)
        tasks = []
        close_code = WSCloseCode.OK
        try:
            await downstream.prepare(request)
            await downstream.send_json({"type": "auth_ok"})
            tasks = [asyncio.create_task(relay_frames(upstream, downstream)),
                     asyncio.create_task(relay_client_frames(downstream, upstream, client_id))]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                try:
                    task.result()
                except SendspinIdentityError:
                    close_code = WSCloseCode.POLICY_VIOLATION
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(upstream.close(), downstream.close(code=close_code),
                                 return_exceptions=True)
        return downstream
