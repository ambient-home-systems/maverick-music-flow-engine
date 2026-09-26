"""Config and options flow tests against a fake Music Assistant server."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import (
    MA_PASSWORD,
    MA_TOKEN,
    MA_USERNAME,
    FakeCommandError,
    FakeMusicAssistant,
    MusicAssistantStub,
)
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maverick_music_flow.const import (
    CONF_ALLOW_LOCAL_MEDIA_URLS,
    CONF_ALLOW_NON_ADMIN_MANAGEMENT,
    CONF_ENABLE_EXPERIMENTAL,
    CONF_INSTANCE_ID,
    CONF_MUSIC_ASSISTANT_EXTERNAL_URL,
    CONF_MUSIC_ASSISTANT_TOKEN,
    CONF_MUSIC_ASSISTANT_URL,
    CONF_PROFILE_ID,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
)

INGRESS_URL = "http://homeassistant.local:8123/api/hassio_ingress/abc123"

pytestmark = pytest.mark.usefixtures("mock_music_assistant")


async def _start(hass: HomeAssistant, step: str) -> dict[str, Any]:
    """Open the setup menu and pick the manual or automatic path."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["automatic", "manual"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": step}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == step
    return result


def _manual_input(fake_ma: FakeMusicAssistant, **overrides: Any) -> dict[str, Any]:
    return {
        "name": "Living room Engine",
        CONF_INSTANCE_ID: "default",
        CONF_PROFILE_ID: "default",
        CONF_MUSIC_ASSISTANT_URL: fake_ma.url,
        CONF_MUSIC_ASSISTANT_EXTERNAL_URL: "",
        CONF_MUSIC_ASSISTANT_TOKEN: MA_TOKEN,
        **overrides,
    }


# --- manual setup ------------------------------------------------------------------------


async def test_manual_setup_creates_entry_and_loads(
    hass: HomeAssistant, fake_ma: FakeMusicAssistant
) -> None:
    """A valid URL and token create an entry that keeps the token in data only."""
    result = await _start(hass, "manual")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_input(fake_ma)
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living room Engine"
    assert result["data"] == {
        CONF_INSTANCE_ID: "default",
        CONF_PROFILE_ID: "default",
        CONF_MUSIC_ASSISTANT_URL: fake_ma.url,
        CONF_MUSIC_ASSISTANT_EXTERNAL_URL: "",
        CONF_MUSIC_ASSISTANT_TOKEN: MA_TOKEN,
    }
    assert result["options"] == {CONF_ENABLE_EXPERIMENTAL: False}
    entry = result["result"]
    assert entry.unique_id == "default"
    assert entry.version == CONFIG_ENTRY_VERSION
    assert entry.state is ConfigEntryState.LOADED
    # The token was checked against the authenticated HTTP API.
    assert fake_ma.commands_named("players/all")
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("overrides", "errors"),
    [
        ({CONF_MUSIC_ASSISTANT_TOKEN: "wrong-token"}, {"base": "invalid_auth"}),
        ({CONF_MUSIC_ASSISTANT_URL: "http://127.0.0.1:9"}, {"base": "cannot_connect"}),
        ({CONF_MUSIC_ASSISTANT_URL: "127.0.0.1:8095"}, {CONF_MUSIC_ASSISTANT_URL: "invalid_url"}),
        ({CONF_MUSIC_ASSISTANT_URL: INGRESS_URL}, {CONF_MUSIC_ASSISTANT_URL: "ma_ingress_url"}),
        (
            {CONF_MUSIC_ASSISTANT_EXTERNAL_URL: "http://music.example.com"},
            {CONF_MUSIC_ASSISTANT_EXTERNAL_URL: "invalid_external_url"},
        ),
        ({CONF_INSTANCE_ID: " "}, {CONF_INSTANCE_ID: "required"}),
    ],
)
async def test_manual_setup_errors(
    hass: HomeAssistant,
    fake_ma: FakeMusicAssistant,
    overrides: dict[str, Any],
    errors: dict[str, str],
) -> None:
    """Invalid input is reported on the form and creates nothing."""
    result = await _start(hass, "manual")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_input(fake_ma, **overrides)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_manual_setup_rejects_ingress_url_without_contacting_it(
    hass: HomeAssistant, fake_ma: FakeMusicAssistant
) -> None:
    """A Home Assistant Ingress path on the MA server's own host is still refused."""
    result = await _start(hass, "manual")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        _manual_input(
            fake_ma, **{CONF_MUSIC_ASSISTANT_URL: f"{fake_ma.url}/api/hassio_ingress/abc"}
        ),
    )
    assert result["errors"] == {CONF_MUSIC_ASSISTANT_URL: "ma_ingress_url"}
    assert fake_ma.commands == []


async def test_manual_setup_rejects_old_music_assistant(
    hass: HomeAssistant, fake_ma: FakeMusicAssistant
) -> None:
    """A server below the supported API schema is refused."""
    fake_ma.schema_version = 62
    result = await _start(hass, "manual")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_input(fake_ma)
    )
    assert result["errors"] == {"base": "unsupported_ma_version"}


async def test_manual_setup_rejects_duplicate_instance(
    hass: HomeAssistant, fake_ma: FakeMusicAssistant, config_entry: MockConfigEntry
) -> None:
    """A second entry for the same instance ID aborts."""
    config_entry.add_to_hass(hass)
    result = await _start(hass, "manual")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_input(fake_ma)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_manual_setup_allows_a_second_instance(
    hass: HomeAssistant, fake_ma: FakeMusicAssistant, config_entry: MockConfigEntry
) -> None:
    """A different instance ID creates a separate entry."""
    config_entry.add_to_hass(hass)
    result = await _start(hass, "manual")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_input(fake_ma, **{CONF_INSTANCE_ID: "upstairs"})
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "upstairs"
    await hass.async_block_till_done()
    # Setting up the domain loaded both entries.
    assert config_entry.state is ConfigEntryState.LOADED
    for entry in hass.config_entries.async_entries(DOMAIN):
        assert await hass.config_entries.async_unload(entry.entry_id)


# --- automatic setup ---------------------------------------------------------------------


async def test_automatic_setup_creates_dedicated_token(
    hass: HomeAssistant, fake_ma: FakeMusicAssistant
) -> None:
    """Signing in creates a dedicated token, stores only it and revokes the login."""
    result = await _start(hass, "automatic")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MUSIC_ASSISTANT_URL: fake_ma.url}
    )
    # The fake server listens on http://, so the form carries the unencrypted warning.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "automatic_login_http"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": MA_USERNAME, "password": MA_PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert fake_ma.created_tokens == ["created-token-1"]
    assert result["data"][CONF_MUSIC_ASSISTANT_TOKEN] == "created-token-1"
    assert result["data"][CONF_MUSIC_ASSISTANT_URL] == fake_ma.url
    assert CONF_MUSIC_ASSISTANT_TOKEN not in result["options"]
    stored = repr(dict(result["result"].data)) + repr(dict(result["result"].options))
    assert MA_PASSWORD not in stored
    assert MA_USERNAME not in stored
    # The temporary login session was logged out, which revokes its token.
    assert fake_ma.revoked_tokens == ["login-token-1"]
    assert result["result"].state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(result["result"].entry_id)


async def test_automatic_setup_rejects_ingress_url(
    hass: HomeAssistant, fake_ma: FakeMusicAssistant
) -> None:
    """An Ingress URL is refused before any sign-in is attempted."""
    result = await _start(hass, "automatic")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MUSIC_ASSISTANT_URL: INGRESS_URL}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "automatic"
    assert result["errors"] == {CONF_MUSIC_ASSISTANT_URL: "ma_ingress_url"}
    assert fake_ma.connections == []


async def test_automatic_setup_wrong_password_creates_nothing(
    hass: HomeAssistant, fake_ma: FakeMusicAssistant
) -> None:
    """A failed sign-in reports an error and leaves no token on MA."""
    result = await _start(hass, "automatic")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MUSIC_ASSISTANT_URL: fake_ma.url}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": MA_USERNAME, "password": "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "automatic_login_failed"}
    assert fake_ma.created_tokens == []
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_automatic_setup_revokes_token_when_validation_fails(
    hass: HomeAssistant, fake_ma: FakeMusicAssistant
) -> None:
    """A token that fails the API check is revoked instead of left behind."""

    def broken_player_list(args: dict[str, Any]) -> Any:
        raise FakeCommandError("players unavailable")

    fake_ma.responses["players/all"] = broken_player_list
    result = await _start(hass, "automatic")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MUSIC_ASSISTANT_URL: fake_ma.url}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": MA_USERNAME, "password": MA_PASSWORD}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert fake_ma.created_tokens == ["created-token-1"]
    assert "created-token-1" in fake_ma.revoked_tokens
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_automatic_setup_rejects_duplicate_before_signing_in(
    hass: HomeAssistant, fake_ma: FakeMusicAssistant, config_entry: MockConfigEntry
) -> None:
    """A duplicate instance aborts before a token is created on MA."""
    config_entry.add_to_hass(hass)
    result = await _start(hass, "automatic")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MUSIC_ASSISTANT_URL: fake_ma.url}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert fake_ma.connections == []
    assert fake_ma.created_tokens == []


# --- options flow ------------------------------------------------------------------------


async def test_options_general_keeps_token_out_of_options_and_reloads(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, fake_ma: FakeMusicAssistant
) -> None:
    """Saving general options without a token keeps the stored one and reloads."""
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "general"}
    )
    assert result["step_id"] == "general"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_PROFILE_ID: "default",
            CONF_MUSIC_ASSISTANT_URL: fake_ma.url,
            CONF_MUSIC_ASSISTANT_EXTERNAL_URL: "",
            CONF_ALLOW_NON_ADMIN_MANAGEMENT: True,
            CONF_ALLOW_LOCAL_MEDIA_URLS: False,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert loaded_entry.options[CONF_ALLOW_NON_ADMIN_MANAGEMENT] is True
    assert CONF_MUSIC_ASSISTANT_TOKEN not in loaded_entry.options
    assert loaded_entry.data[CONF_MUSIC_ASSISTANT_TOKEN] == MA_TOKEN
    # The update listener reloaded the entry with the new option.
    assert loaded_entry.state is ConfigEntryState.LOADED
    assert hass.data[DOMAIN]["runtime"].non_admin_management_allowed() is True


async def test_options_general_replaces_token_in_data(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, fake_ma: FakeMusicAssistant
) -> None:
    """A new token is validated and stored in entry data, never in options."""
    fake_ma.valid_tokens.add("rotated-token")
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "general"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_PROFILE_ID: "default",
            CONF_MUSIC_ASSISTANT_URL: fake_ma.url,
            CONF_MUSIC_ASSISTANT_TOKEN: "rotated-token",
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert loaded_entry.data[CONF_MUSIC_ASSISTANT_TOKEN] == "rotated-token"
    assert CONF_MUSIC_ASSISTANT_TOKEN not in loaded_entry.options


async def test_options_general_rejects_ingress_url_and_bad_token(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, fake_ma: FakeMusicAssistant
) -> None:
    """The options form applies the same URL and token checks as setup."""
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "general"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MUSIC_ASSISTANT_URL: INGRESS_URL}
    )
    assert result["errors"] == {CONF_MUSIC_ASSISTANT_URL: "ma_ingress_url"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_MUSIC_ASSISTANT_URL: fake_ma.url, CONF_MUSIC_ASSISTANT_TOKEN: "not-a-valid-token"},
    )
    assert result["errors"] == {"base": "invalid_auth"}
    assert loaded_entry.data[CONF_MUSIC_ASSISTANT_TOKEN] == MA_TOKEN


async def test_options_add_volume_rule_uses_music_assistant_players(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    mock_music_assistant: MusicAssistantStub,
) -> None:
    """The options menu can add a volume rule for a Music Assistant player."""
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "add_volume_rule"}
    )
    assert result["step_id"] == "add_volume_rule"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"player": "media_player.kitchen", "max_volume": 40, "days": "1,2", "enabled": True},
    )
    # Back at the menu; close it.
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "done"}
    )
    await hass.async_block_till_done()
    rules = hass.data[DOMAIN]["runtime"].volume_rules("default")
    assert [(rule["player"], rule["max_volume"], rule["days"]) for rule in rules] == [
        ("media_player.kitchen", 40, [1, 2])
    ]
