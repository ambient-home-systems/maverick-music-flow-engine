"""One-shot MA login used only during explicit Engine onboarding."""
import asyncio
from urllib.parse import urlsplit, urlunsplit

from .const import MUSIC_ASSISTANT_SCHEMA_MIN


def is_ha_interface_url(url):
    """Recognize HA UI/ingress paths, without guessing from a custom port alone."""
    parsed = urlsplit(url.strip())
    route = (parsed.path + '/' + parsed.fragment).lower()
    return any(part in route for part in (
        '/api/hassio_ingress/', '/hassio/ingress/', '/dashboard-',
        '/lovelace', '/config/', '/app/', '_music_assistant',
    ))


def onboarding_endpoint(url):
    """Return the MA WebSocket endpoint for a server URL, or raise ValueError with an error key."""
    if is_ha_interface_url(url):
        raise ValueError("ma_ingress_url")
    parts = urlsplit(url.strip())
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('invalid_url')
    return urlunsplit(('wss' if parts.scheme == 'https' else 'ws', parts.netloc, parts.path.rstrip('/') + '/ws', '', ''))


async def _command(ws, message_id, name, args):
    """Send one MA command and return its result; any MA error fails the onboarding."""
    await ws.send_json({'message_id': message_id, 'command': name, 'args': args})
    while True:
        result = await ws.receive_json()
        if not isinstance(result, dict) or result.get('message_id') != message_id:
            continue
        if result.get('error') or result.get('error_code'):
            raise ValueError('automatic_login_failed')
        return result.get('result')


async def _logout(ws, message_id):
    """Revoke the token this connection authenticated with, using MA's auth/logout.

    MA deletes the token and then drops every connection that used it, usually before
    the reply is sent, so a closed connection counts as success. Best effort: returns
    False instead of raising.
    """
    try:
        async with asyncio.timeout(5):
            await ws.send_json({'message_id': message_id, 'command': 'auth/logout', 'args': {}})
            while True:
                try:
                    result = await ws.receive_json()
                except TypeError:
                    # aiohttp raises this when the next message is a close frame.
                    return True
                if isinstance(result, dict) and result.get('message_id') == message_id:
                    return not (result.get('error') or result.get('error_code'))
    except Exception:  # noqa: BLE001 - cleanup must never fail the setup step
        return False


async def create_onboarding_token(session, url, username, password):
    """Exchange MA built-in credentials for a dedicated token; never retain credentials."""
    endpoint = onboarding_endpoint(url)
    token = None
    try:
        async with asyncio.timeout(25):
            async with session.ws_connect(endpoint, heartbeat=20) as ws:
                hello = await ws.receive_json()
                if not isinstance(hello, dict) or int(hello.get('schema_version') or hello.get('api_schema_version') or 0) < MUSIC_ASSISTANT_SCHEMA_MIN:
                    raise ValueError('unsupported_ma_version')
                login = await _command(ws, 'homeii_setup_1', 'auth/login', {'username': username, 'password': password, 'provider_id': 'builtin', 'device_name': 'HOMEii Flow Engine setup'})
                if not isinstance(login, dict) or not login.get('success') or not isinstance(login.get('access_token'), str):
                    raise ValueError('automatic_login_failed')
                await _command(ws, 'homeii_setup_2', 'auth', {'token': login['access_token'], 'device_name': 'HOMEii Flow Engine setup'})
                try:
                    created = await _command(ws, 'homeii_setup_3', 'auth/token/create', {'name': 'HOMEii Flow Engine'})
                    if isinstance(created, str) and created.strip():
                        token = created
                finally:
                    # The login token only served this exchange. MA renews it on use, so
                    # revoke it now instead of leaving a session behind.
                    await _logout(ws, 'homeii_setup_4')
    except TimeoutError:
        # Running out of time while logging out must not lose a token MA already created.
        if token is None:
            raise
    if token is None:
        raise ValueError('automatic_login_failed')
    return token


async def revoke_onboarding_token(session, url, token):
    """Revoke a token from create_onboarding_token that will not be used. Best effort.

    auth/token/revoke needs the token's ID, which auth/token/create does not return, so
    this authenticates with the token itself and logs out, which deletes that token.
    """
    try:
        async with asyncio.timeout(15):
            async with session.ws_connect(onboarding_endpoint(url), heartbeat=20) as ws:
                await ws.receive_json()
                await _command(ws, 'homeii_revoke_1', 'auth', {'token': token, 'device_name': 'HOMEii Flow Engine setup'})
                return await _logout(ws, 'homeii_revoke_2')
    except Exception:  # noqa: BLE001 - the caller reports the original setup error
        return False
