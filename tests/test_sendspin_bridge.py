"""Exercise actual Sendspin view and relay with simulated HA/MA boundaries."""
import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock
import re
from urllib.parse import urlsplit, urlunsplit

SOURCE = Path(__file__).resolve().parents[1] / 'custom_components/maverick_music_flow/sendspin_bridge.py'
tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
class HTTPError(Exception):
    def __init__(self, text=''): super().__init__(text)
class HTTPForbidden(HTTPError): pass
class HTTPTooManyRequests(HTTPError): pass
class HTTPServiceUnavailable(HTTPError): pass
class Socket:
    def __init__(self, frames=()):
        self.frames = frames
        self.send_str = AsyncMock()
        self.send_bytes = AsyncMock()
        self.send_json = AsyncMock()
        self.close = AsyncMock()
        self.prepare = AsyncMock()
        self.receive_json = AsyncMock(return_value={'type':'auth_ok'})
    def __aiter__(self):
        async def iterate():
            for frame in self.frames: yield frame
        return iterate()

kinds = SimpleNamespace(TEXT=1, BINARY=2, ERROR=3, CLOSE=4, CLOSED=5)
close_codes = SimpleNamespace(OK=1000, GOING_AWAY=1001, POLICY_VIOLATION=1008)
web = SimpleNamespace(HTTPException=HTTPError, HTTPBadRequest=HTTPError, HTTPBadGateway=HTTPError, HTTPServiceUnavailable=HTTPServiceUnavailable,
                      HTTPForbidden=HTTPForbidden, HTTPTooManyRequests=HTTPTooManyRequests)
ns = dict(asyncio=asyncio, json=json, re=re, urlsplit=urlsplit, urlunsplit=urlunsplit, DOMAIN='maverick_music_flow',
          NOT_LOADED_MESSAGE='HOMEii Flow Engine is not loaded', WSMsgType=kinds,
          WSCloseCode=close_codes, web=web, HomeAssistantView=object)
nodes = [ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)]
nodes += [n for n in tree.body if isinstance(n, (ast.Assign, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),str(SOURCE),'exec'),ns)

def text(data): return SimpleNamespace(type=kinds.TEXT, data=data)
def hello(client_id): return text(json.dumps({'type':'client/hello','payload':{'client_id':client_id,'version':1}}))
def request(user_id): return {'hass_user': SimpleNamespace(id=user_id)}

class BridgeTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.up = Socket()
        self.down = Socket()
        web.WebSocketResponse = lambda **_: self.down
        self.ws_connect = AsyncMock(return_value=self.up)
        ns['async_get_clientsession'] = lambda _: SimpleNamespace(ws_connect=self.ws_connect)
        self.tracked = []
        def track(coro, name, *, background=False):
            self.assertTrue(background)  # relays must not hold up Home Assistant's shutdown
            self.tracked.append(asyncio.create_task(coro, name=name))
            return self.tracked[-1]
        self.runtime = SimpleNamespace(music_assistant_base_urls=lambda:['http://ma:8095'],music_assistant_tokens=lambda:['server-secret'],
                                       _storage={}, async_save=AsyncMock(), active=True, async_create_tracked_task=track)
        self.users = {'alice', 'bob'}
        auth = SimpleNamespace(async_get_user=AsyncMock(side_effect=lambda uid: SimpleNamespace(id=uid) if uid in self.users else None))
        self.view = ns['HomeiiFlowSendspinView'](SimpleNamespace(data={'maverick_music_flow':{'runtime':self.runtime}}, auth=auth))
    def owners(self): return self.runtime._storage['sendspin_clients']
    async def test_text_binary_forwarding_stops_on_close(self):
        source=Socket([SimpleNamespace(type=kinds.TEXT,data='hello'),SimpleNamespace(type=kinds.BINARY,data=b'audio'),SimpleNamespace(type=kinds.CLOSE,data=None),SimpleNamespace(type=kinds.TEXT,data='late')])
        await ns['relay_frames'](source,self.down)
        self.down.send_str.assert_awaited_once_with('hello')
        self.down.send_bytes.assert_awaited_once_with(b'audio')
    async def test_success_authenticates_upstream_only_and_closes_both(self):
        self.assertTrue(self.view.requires_auth)
        await self.view.get(request('alice'),'homeii-test')
        self.up.send_json.assert_awaited_once_with({'type':'auth','token':'server-secret','client_id':'homeii-test'})
        self.down.send_json.assert_awaited_once_with({'type':'auth_ok'})
        self.up.close.assert_awaited_once()
        self.down.close.assert_awaited_once()
    async def test_rejection_never_upgrades_client_connection(self):
        self.up.receive_json.return_value={'type':'auth_invalid'}
        with self.assertRaises(HTTPError): await self.view.get(request('alice'),'device')
        self.down.prepare.assert_not_awaited()
        self.up.close.assert_awaited_once()
    async def test_handshake_cancellation_closes_upstream(self):
        self.up.receive_json.side_effect=asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError): await self.view.get(request('alice'),'device')
        self.up.close.assert_awaited_once()
    async def test_relay_failure_still_closes_both(self):
        self.up.frames=[SimpleNamespace(type=kinds.TEXT,data='frame')]
        self.down.send_str.side_effect=ValueError('transport failure')
        with self.assertRaises(ValueError): await self.view.get(request('alice'),'device')
        self.up.close.assert_awaited_once()
        self.down.close.assert_awaited_once()
    # Engine lifecycle
    async def test_refused_while_no_entry_is_loaded(self):
        self.runtime.active = False
        with self.assertRaises(HTTPServiceUnavailable) as caught: await self.view.get(request('alice'),'device')
        self.assertEqual(str(caught.exception), 'HOMEii Flow Engine is not loaded')
        self.ws_connect.assert_not_awaited()
        self.runtime.async_save.assert_not_awaited()
    async def test_unloading_the_engine_closes_open_relays(self):
        release = asyncio.Event()
        class Blocking(Socket):
            def __aiter__(self):
                async def iterate():
                    await release.wait()
                    return
                    yield
                return iterate()
        self.up, self.down = Blocking(), Blocking()
        self.ws_connect.return_value = self.up
        web.WebSocketResponse = lambda **_: self.down
        relay = asyncio.create_task(self.view.get(request('alice'),'alice-device'))
        while len(self.tracked) < 2: await asyncio.sleep(0)
        for task in self.tracked: task.cancel()  # what the runtime does on unload
        await asyncio.wait_for(relay, 1)
        self.down.close.assert_awaited_once_with(code=close_codes.GOING_AWAY)
        self.up.close.assert_awaited_once()
        self.assertTrue(all(task.done() for task in self.tracked))
        self.assertEqual(self.view.registry._sessions, {})
    async def test_unload_during_handshake_closes_without_relaying(self):
        async def unload(_message): self.runtime.active = False
        self.down.send_json.side_effect = unload
        self.down.frames = [hello('alice-device')]
        await self.view.get(request('alice'),'alice-device')
        self.assertEqual(self.tracked, [])
        self.up.send_str.assert_not_awaited()
        self.down.close.assert_awaited_once_with(code=close_codes.GOING_AWAY)
        self.up.close.assert_awaited_once()
    async def test_rejects_invalid_client_path(self):
        with self.assertRaises(HTTPError): await self.view.get(request('alice'),'../invalid')
        self.up.send_json.assert_not_awaited()

    # Client ownership
    async def test_two_users_cannot_share_a_client_id(self):
        await self.view.get(request('alice'),'ma_homeii_abc')
        self.assertEqual(self.owners(), {'ma_homeii_abc':'alice'})
        self.runtime.async_save.assert_awaited_once()
        self.ws_connect.reset_mock()
        with self.assertRaises(HTTPForbidden): await self.view.get(request('bob'),'ma_homeii_abc')
        self.ws_connect.assert_not_awaited()
        self.assertEqual(self.owners(), {'ma_homeii_abc':'alice'})
        await self.view.get(request('alice'),'ma_homeii_abc')  # the owner can reconnect
        self.runtime.async_save.assert_awaited_once()  # no write for a known owner
        await self.view.get(request('bob'),'ma_homeii_bob')
        self.assertEqual(self.owners(), {'ma_homeii_abc':'alice','ma_homeii_bob':'bob'})
    async def test_ownership_survives_reload_from_storage(self):
        self.runtime._storage['sendspin_clients'] = {'ma_homeii_abc':'alice'}
        with self.assertRaises(HTTPForbidden): await self.view.get(request('bob'),'ma_homeii_abc')
    async def test_client_id_of_deleted_user_can_be_taken_over(self):
        self.runtime._storage['sendspin_clients'] = {'ma_homeii_abc':'removed-user'}
        await self.view.get(request('bob'),'ma_homeii_abc')
        self.assertEqual(self.owners(), {'ma_homeii_abc':'bob'})
    async def test_failed_save_does_not_record_ownership(self):
        self.runtime._storage['sendspin_clients'] = {'ma_homeii_old':'alice'}
        self.runtime.async_save.side_effect = OSError('disk full')
        with self.assertRaises(OSError): await self.view.get(request('bob'),'ma_homeii_abc')
        self.assertEqual(self.owners(), {'ma_homeii_old':'alice'})
        self.ws_connect.assert_not_awaited()
    async def test_each_user_keeps_a_bounded_number_of_client_ids(self):
        limit = ns['MAX_CLIENT_IDS_PER_USER']
        self.runtime._storage['sendspin_clients'] = {'bob-device':'bob'}
        for index in range(limit + 3):
            self.assertTrue(await self.view.registry.async_claim(self.runtime, f'alice-{index}', 'alice'))
        owned = [cid for cid, uid in self.owners().items() if uid == 'alice']
        self.assertEqual(owned, [f'alice-{index}' for index in range(3, limit + 3)])
        self.assertEqual(self.owners()['bob-device'], 'bob')

    # Session caps
    async def test_per_user_session_cap_is_enforced(self):
        registry = self.view.registry
        for _ in range(ns['MAX_SESSIONS_PER_USER']): self.assertTrue(registry.acquire_session('alice'))
        with self.assertRaises(HTTPTooManyRequests): await self.view.get(request('alice'),'alice-device')
        self.ws_connect.assert_not_awaited()
        await self.view.get(request('bob'),'bob-device')  # other users are unaffected
        registry.release_session('alice')
        await self.view.get(request('alice'),'alice-device')
    async def test_total_session_cap_is_enforced(self):
        registry = self.view.registry
        for index in range(ns['MAX_SESSIONS_TOTAL']): self.assertTrue(registry.acquire_session(f'user-{index}'))
        self.assertFalse(registry.acquire_session('alice'))
        with self.assertRaises(HTTPTooManyRequests): await self.view.get(request('alice'),'alice-device')
    async def test_open_relays_count_against_the_cap_and_release_on_close(self):
        release = asyncio.Event()
        class Blocking(Socket):
            def __aiter__(self):
                async def iterate():
                    await release.wait()
                    return
                    yield
                return iterate()
        sockets = []
        def new_socket():
            sockets.append(Blocking()); return sockets[-1]
        self.ws_connect.side_effect = lambda *a, **k: new_socket()
        web.WebSocketResponse = lambda **_: new_socket()
        limit = ns['MAX_SESSIONS_PER_USER']
        relays = [asyncio.create_task(self.view.get(request('alice'),'alice-device')) for _ in range(limit)]
        while len(sockets) < 2 * limit: await asyncio.sleep(0)
        with self.assertRaises(HTTPTooManyRequests): await self.view.get(request('alice'),'alice-device')
        release.set()
        await asyncio.gather(*relays)
        self.assertEqual(self.view.registry._sessions, {})
    async def test_session_slot_released_when_upstream_fails(self):
        self.ws_connect.side_effect = OSError('refused')
        for _ in range(ns['MAX_SESSIONS_PER_USER'] + 1):
            with self.assertRaises(HTTPError): await self.view.get(request('alice'),'alice-device')
        self.assertEqual(self.view.registry._sessions, {})

    # client/hello identity inside the relayed stream
    async def test_matching_hello_is_forwarded(self):
        self.down.frames=[hello('alice-device'), text('{"type":"client/state"}'), SimpleNamespace(type=kinds.BINARY,data=b'x')]
        await self.view.get(request('alice'),'alice-device')
        self.assertEqual(self.up.send_str.await_count, 2)
        self.up.send_bytes.assert_awaited_once_with(b'x')
        self.down.close.assert_awaited_once_with(code=close_codes.OK)
    async def test_hello_for_another_client_id_is_refused(self):
        self.down.frames=[hello('bob-device')]
        await self.view.get(request('alice'),'alice-device')
        self.up.send_str.assert_not_awaited()
        self.down.close.assert_awaited_once_with(code=close_codes.POLICY_VIOLATION)
        self.up.close.assert_awaited_once()
    async def test_frames_before_hello_are_refused(self):
        for first in (text('{"type":"client/time"}'), text('not json'), SimpleNamespace(type=kinds.BINARY,data=b'x')):
            upstream = Socket()
            with self.assertRaises(ns['SendspinIdentityError']):
                await ns['relay_client_frames'](Socket([first, hello('alice-device')]), upstream, 'alice-device')
            upstream.send_str.assert_not_awaited()
            upstream.send_bytes.assert_not_awaited()
    async def test_later_hello_for_another_client_id_is_refused(self):
        upstream = Socket()
        with self.assertRaises(ns['SendspinIdentityError']):
            await ns['relay_client_frames'](Socket([hello('alice-device'), hello('bob-device')]), upstream, 'alice-device')
        upstream.send_str.assert_awaited_once()

if __name__ == '__main__': main()
