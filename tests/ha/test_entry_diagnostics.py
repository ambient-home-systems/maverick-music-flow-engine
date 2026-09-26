"""Config entry diagnostics, downloaded through Home Assistant's diagnostics API."""

from __future__ import annotations

import json
from http import HTTPStatus

from conftest import KITCHEN, MA_TOKEN, FakeMusicAssistant
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.maverick_music_flow.const import DOMAIN, VERSION


async def _download(
    hass: HomeAssistant, client_factory: ClientSessionGenerator, entry: MockConfigEntry
) -> dict:
    """Download diagnostics the way the "Download diagnostics" button does."""
    assert await async_setup_component(hass, "diagnostics", {})
    client = await client_factory()
    response = await client.get(f"/api/diagnostics/config_entry/{entry.entry_id}")
    assert response.status == HTTPStatus.OK
    return (await response.json())["data"]


async def test_diagnostics_content(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    entity_registry: er.EntityRegistry,
) -> None:
    """Diagnostics describe the entry and match the entities that actually exist."""
    await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {
            "id": "wake",
            "name": "Wake up",
            "player": KITCHEN,
            "media_id": "library://playlist/1",
            "time": "07:00",
        },
        blocking=True,
    )
    data = await _download(hass, hass_client, loaded_entry)

    assert data["version"] == VERSION
    assert data["entry"]["instance_id"] == "default"
    assert data["required_connections"]["ok"] is True
    assert data["schedules_count"] == 1
    assert data["next_schedule"]["id"] == "wake"

    # The documented key lists match the fixed entities that were registered (B-25).
    fixed_keys = {
        entity.unique_id.removeprefix(f"{loaded_entry.entry_id}_")
        for entity in er.async_entries_for_config_entry(entity_registry, loaded_entry.entry_id)
    }
    for platform in ("sensor", "binary_sensor", "button", "calendar"):
        documented = set(data[f"{platform}_keys"])
        registered = {
            key
            for key in fixed_keys
            if entity_registry.async_get_entity_id(
                platform, DOMAIN, f"{loaded_entry.entry_id}_{key}"
            )
            and not key.startswith("run_schedule_")
        }
        assert documented == registered, platform


async def test_diagnostics_are_redacted(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    fake_ma: FakeMusicAssistant,
) -> None:
    """No token, MA URL or working artwork link leaves Home Assistant (S-4)."""
    # Make sure the players carry proxied artwork links before downloading.
    await hass.data[DOMAIN]["runtime"].async_players_snapshot()
    text = json.dumps(await _download(hass, hass_client, loaded_entry))
    assert MA_TOKEN not in text
    assert fake_ma.url not in text
    assert f"/api/{DOMAIN}/artwork/item/" not in text
    assert "127.0.0.1" not in text
