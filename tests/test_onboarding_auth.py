"""Onboarding credential exchange contract without a live MA account."""
import ast
import asyncio
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from unittest import IsolatedAsyncioTestCase

source = Path(__file__).resolve().parents[1] / 'custom_components/maverick_music_flow/onboarding_auth.py'
nodes = [n for n in ast.parse(source.read_text()).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
ns = dict(asyncio=asyncio, urlsplit=urlsplit, urlunsplit=urlunsplit, MUSIC_ASSISTANT_SCHEMA_MIN=63)
exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), ns)

class Socket:
    """Stand-in MA WebSocket. logout='close' mimics MA dropping the connection before replying."""
    def __init__(self, denied=False, logout='reply', create='dedicated', auth_ok=True):
        self.sent=[]; self.denied=denied; self.logout=logout; self.create=create; self.auth_ok=auth_ok
    async def __aenter__(self): return self
    async def __aexit__(self,*args): pass
    async def send_json(self,data): self.sent.append(data)
    async def receive_json(self):
        if not self.sent: return {'schema_version':63}
        req=self.sent[-1]
        if req['command']=='auth/logout':
            if self.logout=='close': raise TypeError('Received message 8:1000 is not WSMsgType.TEXT')
            if self.logout=='error': return {'message_id':req['message_id'],'error_code':20,'details':'Not authenticated'}
            return {'message_id':req['message_id'],'result':None}
        if req['command']=='auth' and not self.auth_ok:
            return {'message_id':req['message_id'],'error_code':20,'details':'Invalid or expired token'}
        if req['command']=='auth/token/create' and isinstance(self.create,Exception):
            return {'message_id':req['message_id'],'error_code':3,'details':'Long-lived tokens cannot be created'}
        result={'auth/login':{'success':not self.denied,'access_token':'short'},'auth':{},'auth/token/create':self.create}[req['command']]
        return {'message_id':req['message_id'],'result':result}
class Session:
    def __init__(self,**kw): self.ws=Socket(**kw)
    def ws_connect(self,url,**kw): self.url=url; return self.ws

def commands(session): return [x['command'] for x in session.ws.sent]

class OnboardingTests(IsolatedAsyncioTestCase):
    async def test_exchange_uses_dedicated_token(self):
        session=Session()
        token=await ns['create_onboarding_token'](session,'http://ma:8095','user','secret')
        self.assertEqual(token,'dedicated')
        self.assertEqual(session.url,'ws://ma:8095/ws')
        self.assertEqual(commands(session),['auth/login','auth','auth/token/create','auth/logout'])
        self.assertNotIn('password',session.ws.sent[2]['args'])
    async def test_login_session_is_logged_out_after_token_creation(self):
        # MA revokes the token the connection authenticated with, then drops the socket.
        session=Session(logout='close')
        self.assertEqual(await ns['create_onboarding_token'](session,'https://ma:8095','user','secret'),'dedicated')
        self.assertEqual(session.url,'wss://ma:8095/ws')
        self.assertEqual(commands(session)[-1],'auth/logout')
        self.assertEqual(session.ws.sent[-1]['args'],{})
    async def test_logout_failure_does_not_lose_the_token(self):
        self.assertEqual(await ns['create_onboarding_token'](Session(logout='error'),'http://ma:8095','user','secret'),'dedicated')
    async def test_logout_is_attempted_when_token_creation_fails(self):
        for create in (ValueError('denied'), '', None):
            session=Session(create=create)
            with self.assertRaisesRegex(ValueError,'automatic_login_failed'):
                await ns['create_onboarding_token'](session,'http://ma:8095','user','secret')
            self.assertEqual(commands(session),['auth/login','auth','auth/token/create','auth/logout'])
    async def test_failed_login_does_not_create_token(self):
        session=Session(denied=True)
        with self.assertRaisesRegex(ValueError,'automatic_login_failed'):
            await ns['create_onboarding_token'](session,'http://ma:8095','user','bad')
        self.assertEqual(len(session.ws.sent),1)
    async def test_rejects_credentials_and_fragments_in_url(self):
        for url in ['http://user:pass@ma:8095','http://ma:8095/#/home','file:///tmp']:
            with self.assertRaisesRegex(ValueError,'invalid_url'):
                await ns['create_onboarding_token'](Session(),url,'user','secret')
            with self.assertRaisesRegex(ValueError,'invalid_url'):
                ns['onboarding_endpoint'](url)

    async def test_ingress_has_specific_error(self):
        for url in ['http://ha:8123/api/hassio_ingress/abc/', 'http://ha:8123/d5369777_music_assistant_beta#/', 'http://ha:8123/dashboard-clean/ma']:
            with self.assertRaisesRegex(ValueError,'ma_ingress_url'):
                await ns['create_onboarding_token'](Session(),url,'user','secret')

class RevokeTests(IsolatedAsyncioTestCase):
    async def test_revoke_authenticates_with_the_token_and_logs_out(self):
        for logout in ('reply','close'):
            session=Session(logout=logout)
            self.assertTrue(await ns['revoke_onboarding_token'](session,'http://ma:8095/','dedicated'))
            self.assertEqual(session.url,'ws://ma:8095/ws')
            self.assertEqual(commands(session),['auth','auth/logout'])
            self.assertEqual(session.ws.sent[0]['args']['token'],'dedicated')
    async def test_revoke_reports_failure_without_raising(self):
        self.assertFalse(await ns['revoke_onboarding_token'](Session(auth_ok=False),'http://ma:8095','dedicated'))
        self.assertFalse(await ns['revoke_onboarding_token'](Session(logout='error'),'http://ma:8095','dedicated'))
        class Down:
            def ws_connect(self,url,**kw): raise ConnectionError('refused')
        self.assertFalse(await ns['revoke_onboarding_token'](Down(),'http://ma:8095','dedicated'))
