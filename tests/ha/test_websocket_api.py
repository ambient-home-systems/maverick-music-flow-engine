"""A representative set of Engine WebSocket commands, including authorization (S-3)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import (
    BEDROOM,
    KITCHEN,
    KITCHEN_ID,
    MA_TOKEN,
    FakeCommandError,
    FakeMusicAssistant,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import MockHAClientWebSocket, WebSocketGenerator

from custom_components.maverick_music_flow import websocket_api
from custom_components.maverick_music_flow.const import DOMAIN, VERSION

ITEM_ARTWORK_PREFIX = f"/api/{DOMAIN}/artwork/item/"


async def _command(client: MockHAClientWebSocket, name: str, /, **data: Any) -> dict[str, Any]:
    """Send one Engine command and return the full response message."""
    await client.send_json_auto_id({"type": f"{DOMAIN}/{name}", **data})
    return await client.receive_json()


async def _result(client: MockHAClientWebSocket, name: str, /, **data: Any) -> Any:
    response = await _command(client, name, **data)
    assert response["success"], response
    return response["result"]


@pytest.fixture
async def admin_ws(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> MockHAClientWebSocket:
    """Return a WebSocket client authenticated as an administrator."""
    return await hass_ws_client(hass)


@pytest.fixture
async def kitchen_ws(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    kitchen_user_token: str,
) -> MockHAClientWebSocket:
    """Return a WebSocket client for a non-admin user who controls only the kitchen."""
    return await hass_ws_client(hass, access_token=kitchen_user_token)


async def test_get_context(admin_ws: MockHAClientWebSocket, loaded_entry: MockConfigEntry) -> None:
    """The card's context lists the loaded entry and never includes the MA token."""
    result = await _result(admin_ws, "get_context")
    assert result["version"] == VERSION
    assert result["capabilities"]["user_authorization"] is True
    assert [entry["entry_id"] for entry in result["entries"]] == [loaded_entry.entry_id]
    assert result["music_assistant"]["authenticated"] is True
    assert MA_TOKEN not in json.dumps(result)


async def test_bootstrap_and_players(admin_ws: MockHAClientWebSocket) -> None:
    """Native MA players are mapped to their Home Assistant entities."""
    result = await _result(admin_ws, "bootstrap/get")
    assert result["bootstrap"] is True
    assert result["required_connections"]["ok"] is True
    players = {player["entity_id"]: player for player in result["player_snapshot"]["players"]}
    assert set(players) == {KITCHEN, BEDROOM}
    assert players[KITCHEN]["state"] == "playing"
    assert players[KITCHEN]["volume_level"] == 0.3
    # MA artwork URLs are replaced by the Engine's opaque artwork proxy URLs.
    assert players[KITCHEN]["homeii_artwork_url"].startswith(ITEM_ARTWORK_PREFIX)

    result = await _result(admin_ws, "players/get")
    assert result["music_assistant_count"] == 2


async def test_players_get_reports_music_assistant_errors(
    admin_ws: MockHAClientWebSocket, fake_ma: FakeMusicAssistant
) -> None:
    """A Music Assistant error is returned to the card as a command error."""

    def fail(args: dict[str, Any]) -> Any:
        raise FakeCommandError("Player manager not ready")

    fake_ma.responses["players/all"] = fail
    response = await _command(admin_ws, "players/get")
    assert not response["success"]
    assert response["error"]["code"] == "players_failed"
    assert "Player manager not ready" in response["error"]["message"]


async def test_queue_get(admin_ws: MockHAClientWebSocket, fake_ma: FakeMusicAssistant) -> None:
    """The queue is read from Music Assistant, including chunked list responses."""
    fake_ma.queue_items[KITCHEN_ID].append(
        {
            "queue_item_id": "qi-3",
            "queue_id": KITCHEN_ID,
            "name": "Artist One - Song Three",
            "duration": 150,
        }
    )
    result = await _result(admin_ws, "queue/get", entity_id=KITCHEN)
    assert [item["queue_item_id"] for item in result["items"]] == ["qi-1", "qi-2", "qi-3"]
    assert result["items"][0]["homeii_artwork_url"].startswith(ITEM_ARTWORK_PREFIX)
    assert fake_ma.commands_named("player_queues/items")


async def test_search_and_favorites(admin_ws: MockHAClientWebSocket) -> None:
    """Search and favorites return normalized library items."""
    result = await _result(admin_ws, "search/get", query="song")
    assert [item["name"] for item in result["items"]][:1] == ["Song One"]
    result = await _result(admin_ws, "favorites/get")
    assert [item["name"] for item in result["items"]] == ["Morning Mix"]


async def test_library_get(admin_ws: MockHAClientWebSocket) -> None:
    """The library shelf for playlists is served over WebSocket."""
    result = await _result(admin_ws, "library/get", media_type="playlist")
    assert [item["name"] for item in result["items"]] == ["Morning Mix"]


def test_every_command_is_registered_under_its_name() -> None:
    """Each handler's command type is its own string, never a merged-in schema value."""
    handlers = [
        handler
        for handler in vars(websocket_api).values()
        if callable(handler) and hasattr(handler, "_ws_command")
    ]
    assert len(handlers) > 40
    for handler in handlers:
        assert isinstance(handler._ws_command, str), handler.__name__
        assert handler._ws_command.startswith(f"{DOMAIN}/"), handler.__name__


async def test_ma_command_bridge(
    admin_ws: MockHAClientWebSocket, fake_ma: FakeMusicAssistant
) -> None:
    """Allowlisted MA commands pass through; everything else is refused (S-2)."""
    result = await _result(admin_ws, "ma/command", command="players/all", args={})
    assert [player["player_id"] for player in result["data"]] == [KITCHEN_ID, "ma_bedroom"]

    for command in (
        "config/core/save",
        "providers/remove",
        "music/start_sync",
        "auth/token/create",
    ):
        response = await _command(admin_ws, "ma/command", command=command, args={})
        assert not response["success"], command
        assert response["error"]["code"] == "unauthorized"
        # An administrator is refused too, so the reason must not ask for admin access.
        assert response["error"]["message"] == "This command is not allowed"
    assert not any(
        name.startswith(("config/", "providers/", "auth/")) for name, _ in fake_ma.commands
    )


async def test_player_command(admin_ws: MockHAClientWebSocket, fake_ma: FakeMusicAssistant) -> None:
    """Player commands reach Music Assistant with the native player ID."""
    result = await _result(admin_ws, "player/command", entity_id=KITCHEN, command="pause")
    assert result["ok"] is True
    assert fake_ma.commands_named("players/cmd/pause") == [{"player_id": KITCHEN_ID}]


async def test_schedules_round_trip(admin_ws: MockHAClientWebSocket) -> None:
    """Schedules can be written, read back and deleted over WebSocket."""
    saved = await _result(
        admin_ws,
        "schedules/set",
        schedule_id="wake",
        name="Wake up",
        player=KITCHEN,
        media_id="library://playlist/1",
        time="07:00",
        days=[1, 2, 3, 4, 5],
    )
    assert saved["id"] == "wake"
    result = await _result(admin_ws, "schedules/get")
    assert [schedule["id"] for schedule in result["schedules"]] == ["wake"]
    assert result["next_schedule"]["id"] == "wake"
    removed = await _result(admin_ws, "schedules/delete", schedule_id="wake")
    assert removed["removed"] == 1
    assert (await _result(admin_ws, "schedules/get"))["schedules"] == []


async def test_diagnostics_run_is_redacted(
    admin_ws: MockHAClientWebSocket, fake_ma: FakeMusicAssistant
) -> None:
    """The diagnostics command never returns the token, MA URLs or working artwork links."""
    result = await _result(admin_ws, "diagnostics/run")
    text = json.dumps(result)
    assert MA_TOKEN not in text
    assert fake_ma.url not in text
    assert ITEM_ARTWORK_PREFIX not in text
    assert result["required_connections"]["ok"] is True


async def test_queue_settings_read_and_write(
    admin_ws: MockHAClientWebSocket, kitchen_ws: MockHAClientWebSocket, fake_ma: FakeMusicAssistant
) -> None:
    """Everyone can read queue settings; only administrators can change them."""
    result = await _result(kitchen_ws, "queue/settings")
    assert result["can_edit"] is False
    assert result["entries"]["crossfade_duration"]["value"] == 8

    response = await _command(kitchen_ws, "queue/settings", values={"crossfade_duration": 4})
    assert response["error"]["code"] == "unauthorized"
    assert fake_ma.commands_named("config/core/save") == []

    result = await _result(admin_ws, "queue/settings", values={"crossfade_duration": 4})
    assert result["saved"] is True
    assert fake_ma.queue_config["crossfade_duration"]["value"] == 4


# --- authorization (S-3) -----------------------------------------------------------------


async def test_non_admin_can_read_and_control_permitted_players(
    kitchen_ws: MockHAClientWebSocket, fake_ma: FakeMusicAssistant
) -> None:
    """Reads are open to every user; control follows the entity permission."""
    assert (await _result(kitchen_ws, "get_context"))["available"] is True
    assert (await _result(kitchen_ws, "schedules/get"))["schedules"] == []

    await _result(kitchen_ws, "player/command", entity_id=KITCHEN, command="pause")
    response = await _command(kitchen_ws, "player/command", entity_id=BEDROOM, command="pause")
    assert response["error"]["code"] == "unauthorized"
    # The same check applies to MA commands sent through the bridge.
    response = await _command(
        kitchen_ws, "ma/command", command="players/cmd/pause", args={"player_id": "ma_bedroom"}
    )
    assert response["error"]["code"] == "unauthorized"
    assert fake_ma.commands_named("players/cmd/pause") == [{"player_id": KITCHEN_ID}]


@pytest.mark.parametrize(
    ("command", "data"),
    [
        ("schedules/set", {"schedule_id": "wake", "player": KITCHEN, "time": "07:00"}),
        ("timers/set", {"player": KITCHEN, "minutes": 5}),
        ("volume_rules/set", {"player": KITCHEN, "max_volume": 10}),
        ("orchestration/run_once", {}),
        ("screensaver/set", {"enabled": True}),
        ("lighting/set", {"player": KITCHEN, "enabled": False}),
        ("interface/set", {"night_mode": "on"}),
    ],
)
async def test_management_commands_are_admin_only(
    hass: HomeAssistant,
    admin_ws: MockHAClientWebSocket,
    kitchen_ws: MockHAClientWebSocket,
    command: str,
    data: dict[str, Any],
) -> None:
    """Non-admins cannot manage schedules or change configuration, even for their players."""
    response = await _command(kitchen_ws, command, **data)
    assert not response["success"]
    assert response["error"]["code"] == "unauthorized"
    runtime = hass.data[DOMAIN]["runtime"]
    assert runtime.schedules() == []
    assert runtime.timers() == []
    assert runtime.volume_rules() == []
