"""HTTP views: the command view, both artwork proxies and the static frontend files."""

from __future__ import annotations

from http import HTTPStatus

from conftest import (
    BEDROOM_ID,
    KITCHEN,
    KITCHEN_ID,
    MA_TOKEN,
    FakeMusicAssistant,
    engine_runtime,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.maverick_music_flow.const import DOMAIN, VERSION

COMMAND_URL = f"/api/{DOMAIN}/command"


async def _kitchen_artwork_url(hass: HomeAssistant) -> str:
    """Return the opaque artwork URL the Engine hands the card for the kitchen player."""
    snapshot = await engine_runtime(hass).async_players_snapshot()
    kitchen = next(player for player in snapshot["players"] if player["entity_id"] == KITCHEN)
    url = kitchen["homeii_artwork_url"]
    assert url.startswith(f"/api/{DOMAIN}/artwork/item/")
    return url


# --- command view --------------------------------------------------------------------------


async def test_command_view_runs_reads(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, hass_client: ClientSessionGenerator
) -> None:
    """The card's HTTP fallback answers reads with the same payloads as WebSocket."""
    client = await hass_client()
    response = await client.post(
        f"{COMMAND_URL}/get_context", json={"type": f"{DOMAIN}/get_context"}
    )
    assert response.status == HTTPStatus.OK
    body = await response.json()
    assert body["version"] == VERSION
    assert MA_TOKEN not in await response.text()

    # library/get works over HTTP, which is how the card reaches it today.
    response = await client.post(f"{COMMAND_URL}/library/get", json={"media_type": "playlist"})
    assert response.status == HTTPStatus.OK
    assert [item["name"] for item in (await response.json())["items"]] == ["Morning Mix"]


async def test_command_view_rejects_bad_requests(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    fake_ma: FakeMusicAssistant,
) -> None:
    """Unknown commands and keys, denied MA commands and anonymous callers are refused."""
    anonymous = await hass_client_no_auth()
    assert (
        await anonymous.post(f"{COMMAND_URL}/get_context", json={})
    ).status == HTTPStatus.UNAUTHORIZED

    client = await hass_client()
    assert (
        await client.post(f"{COMMAND_URL}/schedules/set", json={})
    ).status == HTTPStatus.NOT_FOUND
    response = await client.post(f"{COMMAND_URL}/get_context", json={"unexpected": 1})
    assert response.status == HTTPStatus.BAD_REQUEST
    response = await client.post(
        f"{COMMAND_URL}/ma/command", json={"command": "config/core/save", "args": {}}
    )
    assert response.status == HTTPStatus.FORBIDDEN
    assert fake_ma.commands_named("config/core/save") == []


async def test_command_view_ignores_internal_flags(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, hass_client: ClientSessionGenerator
) -> None:
    """Engine-internal _homeii_* keys from callers are dropped, not rejected (S-2)."""
    client = await hass_client()
    response = await client.post(
        f"{COMMAND_URL}/ma/command",
        json={
            "command": "players/all",
            "args": {},
            "_homeii_cache_worker": True,
            "_homeii_cache_refresh": True,
        },
    )
    assert response.status == HTTPStatus.OK
    assert (await response.json())["command"] == "players/all"


async def test_command_view_applies_entity_permissions(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    kitchen_user_token: str,
    fake_ma: FakeMusicAssistant,
) -> None:
    """A non-admin may control the players they are allowed to, and no others (S-3)."""
    client = await hass_client(kitchen_user_token)
    response = await client.post(
        f"{COMMAND_URL}/ma/command",
        json={"command": "players/cmd/pause", "args": {"player_id": KITCHEN_ID}},
    )
    assert response.status == HTTPStatus.OK
    response = await client.post(
        f"{COMMAND_URL}/ma/command",
        json={"command": "players/cmd/pause", "args": {"player_id": BEDROOM_ID}},
    )
    assert response.status == HTTPStatus.FORBIDDEN
    assert fake_ma.commands_named("players/cmd/pause") == [{"player_id": KITCHEN_ID}]


# --- artwork proxies (S-1) -------------------------------------------------------------------


async def test_item_artwork_is_served_without_login(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    fake_ma: FakeMusicAssistant,
) -> None:
    """An opaque token serves validated MA artwork with hardened headers."""
    url = await _kitchen_artwork_url(hass)
    client = await hass_client_no_auth()
    response = await client.get(url)
    assert response.status == HTTPStatus.OK
    assert response.content_type == "image/png"
    assert await response.read() == fake_ma.image
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
    assert response.headers["X-HOMEii-Flow-Artwork-Source"] == "item-proxy"
    # The MA token is sent to the configured MA server only.
    assert fake_ma.image_requests[-1]["authorization"] == f"Bearer {MA_TOKEN}"
    etag = response.headers["ETag"]

    fetches = len(fake_ma.image_requests)
    response = await client.get(url, headers={"If-None-Match": etag})
    assert response.status == HTTPStatus.NOT_MODIFIED
    response = await client.get(url)
    assert response.headers["X-HOMEii-Flow-Artwork-Source"] == "memory-cache"
    assert len(fake_ma.image_requests) == fetches


async def test_item_artwork_refuses_unknown_tokens_and_private_sources(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    fake_ma: FakeMusicAssistant,
) -> None:
    """Unknown tokens and sources on private addresses other than MA are not fetched."""
    client = await hass_client_no_auth()
    response = await client.get(f"/api/{DOMAIN}/artwork/item/{'0' * 36}")
    assert response.status == HTTPStatus.NOT_FOUND

    runtime = engine_runtime(hass)
    for source in (
        "http://192.168.1.20/cover.png",
        "http://localhost:8123/local/cover.png",
        "http://[::1]/cover.png",
    ):
        url = runtime.register_artwork_source(source)
        response = await client.get(url)
        assert response.status == HTTPStatus.NOT_FOUND, source
    assert fake_ma.image_requests == []


async def test_item_artwork_stops_after_unload(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, hass_client_no_auth: ClientSessionGenerator
) -> None:
    """The login-free artwork route answers like an unknown token once unloaded (S-9)."""
    url = await _kitchen_artwork_url(hass)
    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    client = await hass_client_no_auth()
    assert (await client.get(url)).status == HTTPStatus.NOT_FOUND


async def test_player_artwork_needs_login(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    fake_ma: FakeMusicAssistant,
) -> None:
    """The per-player artwork route requires a login and resolves the current artwork."""
    await _kitchen_artwork_url(hass)  # The Engine learns the player's artwork from MA.
    url = f"/api/{DOMAIN}/artwork/{KITCHEN}"
    anonymous = await hass_client_no_auth()
    assert (await anonymous.get(url)).status == HTTPStatus.UNAUTHORIZED

    client = await hass_client()
    response = await client.get(url)
    assert response.status == HTTPStatus.OK
    assert await response.read() == fake_ma.image
    assert response.headers["Cache-Control"] == "no-store, max-age=0"
    assert (
        await client.get(f"/api/{DOMAIN}/artwork/media_player.unknown")
    ).status == HTTPStatus.NOT_FOUND


async def test_static_frontend_files(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, hass_client_no_auth: ClientSessionGenerator
) -> None:
    """The screensaver script and brand images referenced in the context are served."""
    client = await hass_client_no_auth()
    context = engine_runtime(hass).context()
    for url in context["frontend"].values():
        response = await client.get(url)
        assert response.status == HTTPStatus.OK, url
