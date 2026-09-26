"""Setup, unload, reload and migration of the Engine config entry."""

from __future__ import annotations

from http import HTTPStatus

import pytest
from conftest import (
    KITCHEN,
    MA_TOKEN,
    FakeMusicAssistant,
    MusicAssistantStub,
    async_setup_engine,
    async_wait_for,
    engine_runtime,
    music_assistant_status,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator, WebSocketGenerator

from custom_components.maverick_music_flow.const import (
    CONF_ENABLE_EXPERIMENTAL,
    CONF_INSTANCE_ID,
    CONF_MUSIC_ASSISTANT_TOKEN,
    DOMAIN,
    EVENT_MUSIC_ASSISTANT,
    PLATFORMS,
)

STATUS_SENSOR = "sensor.homeii_flow_engine_status"
CONNECTIONS_OK = "binary_sensor.homeii_flow_engine_required_connections_ok"


async def test_setup_starts_runtime_and_connects(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    fake_ma: FakeMusicAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Setup loads every platform, one device and one authenticated MA connection."""
    runtime = engine_runtime(hass)
    assert runtime.active
    assert [entry.entry_id for entry in runtime.entries] == [loaded_entry.entry_id]
    assert len(fake_ma.authenticated_connections) == 1
    assert fake_ma.authenticated_connections[0].token == MA_TOKEN

    device = device_registry.async_get_device(identifiers={(DOMAIN, loaded_entry.entry_id)})
    assert device is not None
    assert device.manufacturer == "HOMEii"
    entities = er.async_entries_for_config_entry(entity_registry, loaded_entry.entry_id)
    assert {entity.domain for entity in entities} == set(PLATFORMS)
    assert all(entity.device_id == device.id for entity in entities)

    assert hass.states.get(STATUS_SENSOR).state == "connected"
    assert hass.states.get(CONNECTIONS_OK).state == "on"
    snapshot = runtime.required_connections_snapshot()
    assert snapshot["ok"], snapshot["summary"]
    assert snapshot["music_assistant"]["music_assistant_player_count"] == 2


async def test_setup_with_a_rejected_token_still_loads(
    hass: HomeAssistant,
    mock_music_assistant: MusicAssistantStub,
    config_entry: MockConfigEntry,
) -> None:
    """MA connection problems never fail setup; the connection sensors report them."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, data={**config_entry.data, CONF_MUSIC_ASSISTANT_TOKEN: "rejected-token"}
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(CONNECTIONS_OK).state == "off"
    assert not music_assistant_status(hass)["authenticated"]
    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_unload_stops_all_engine_work(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    fake_ma: FakeMusicAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client: ClientSessionGenerator,
) -> None:
    """After unload nothing runs and every endpoint refuses requests (B-2, S-9).

    The autouse cleanup check of the test harness also fails the test if a timer,
    task or thread is left behind.
    """
    runtime = engine_runtime(hass)
    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()

    assert loaded_entry.state is ConfigEntryState.NOT_LOADED
    assert not runtime.active
    assert runtime.orchestration_status()["running"] is False
    await async_wait_for(lambda: not fake_ma.authenticated_connections, message="MA socket closed")
    engine_states = [
        state
        for state in hass.states.async_all()
        if state.entity_id.split(".", 1)[1].startswith("homeii_flow_engine")
    ]
    assert len(engine_states) > 30
    assert {state.state for state in engine_states} == {STATE_UNAVAILABLE}

    # Services stay registered so automations validate, but refuse to run.
    assert hass.services.has_service(DOMAIN, "set_schedule")
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, "run_orchestration", {}, blocking=True)
    assert err.value.translation_key == "not_loaded"

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/get_context"})
    response = await client.receive_json()
    assert not response["success"]
    assert response["error"]["code"] == "not_loaded"

    http = await hass_client()
    response = await http.post(f"/api/{DOMAIN}/command/get_context", json={})
    assert response.status == HTTPStatus.SERVICE_UNAVAILABLE


async def test_removing_the_entry_removes_its_entities_and_device(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Deleting the integration leaves no Engine entities or device behind."""
    await hass.services.async_call(
        DOMAIN, "set_volume_rule", {"player": KITCHEN, "max_volume": 90}, blocking=True
    )
    await hass.async_block_till_done()
    entity_ids = [
        entity.entity_id
        for entity in er.async_entries_for_config_entry(entity_registry, loaded_entry.entry_id)
    ]
    assert entity_ids

    assert (await hass.config_entries.async_remove(loaded_entry.entry_id))[
        "require_restart"
    ] is False
    await hass.async_block_till_done()
    assert er.async_entries_for_config_entry(entity_registry, loaded_entry.entry_id) == []
    assert device_registry.async_get_device(identifiers={(DOMAIN, loaded_entry.entry_id)}) is None
    assert all(hass.states.get(entity_id) is None for entity_id in entity_ids)
    assert not engine_runtime(hass).active


async def test_reload_keeps_one_connection_and_one_set_of_entities(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    fake_ma: FakeMusicAssistant,
    entity_registry: er.EntityRegistry,
) -> None:
    """Reloading replaces the MA connection and entities instead of adding more."""
    before = {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(entity_registry, loaded_entry.entry_id)
    }

    assert await hass.config_entries.async_reload(loaded_entry.entry_id)
    await hass.async_block_till_done()
    runtime = engine_runtime(hass)
    await async_wait_for(lambda: music_assistant_status(hass)["authenticated"])
    await async_wait_for(lambda: len(fake_ma.authenticated_connections) == 1)

    assert loaded_entry.state is ConfigEntryState.LOADED
    assert runtime.active
    assert len(runtime.entries) == 1
    after = {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(entity_registry, loaded_entry.entry_id)
    }
    assert after == before
    assert runtime.orchestration_status()["running"] is True


async def test_unloading_one_of_two_entries_keeps_the_engine_running(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    fake_ma: FakeMusicAssistant,
) -> None:
    """The shared runtime stops only when the last entry unloads."""
    second = MockConfigEntry(
        domain=DOMAIN,
        title="Upstairs",
        version=loaded_entry.version,
        unique_id="upstairs",
        data={**loaded_entry.data, CONF_INSTANCE_ID: "upstairs"},
        options={CONF_ENABLE_EXPERIMENTAL: False},
    )
    await async_setup_engine(hass, second)
    runtime = engine_runtime(hass)
    assert len(runtime.entries) == 2

    assert await hass.config_entries.async_unload(second.entry_id)
    await hass.async_block_till_done()
    assert runtime.active
    assert [entry.entry_id for entry in runtime.entries] == [loaded_entry.entry_id]
    assert music_assistant_status(hass)["authenticated"]


async def test_music_assistant_events_reach_the_bus(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, fake_ma: FakeMusicAssistant
) -> None:
    """Events from the MA WebSocket are forwarded as Home Assistant bus events."""
    events = []
    hass.bus.async_listen(EVENT_MUSIC_ASSISTANT, events.append)
    await fake_ma.async_send_event("player_updated", object_id="ma_kitchen")
    await async_wait_for(lambda: any(event.data.get("kind") == "event" for event in events))

    event = next(event for event in events if event.data.get("kind") == "event")
    assert event.data["event"] == "player_updated"
    assert event.data["object_id"] == "ma_kitchen"
    # Only the redacted connection status is ever published.
    for item in events:
        assert MA_TOKEN not in repr(item.data)


async def test_music_assistant_reconnects_after_server_restart(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, fake_ma: FakeMusicAssistant
) -> None:
    """A dropped MA connection is re-established and re-authenticated."""
    await fake_ma.async_drop_connections()
    await async_wait_for(lambda: len(fake_ma.authenticated_connections) == 1, timeout=10)
    runtime = engine_runtime(hass)
    await async_wait_for(lambda: music_assistant_status(hass)["authenticated"])
    result = await runtime.async_players_snapshot()
    assert {player["entity_id"] for player in result["players"]} == {
        KITCHEN,
        "media_player.bedroom",
    }


async def test_version_1_entry_is_migrated(
    hass: HomeAssistant,
    mock_music_assistant: MusicAssistantStub,
    config_entry: MockConfigEntry,
) -> None:
    """Version 1 kept the token in options; migration moves it into data."""
    data = dict(config_entry.data)
    data[CONF_MUSIC_ASSISTANT_TOKEN] = "stale-data-token"
    old_entry = MockConfigEntry(
        domain=DOMAIN,
        title=config_entry.title,
        version=1,
        unique_id=config_entry.unique_id,
        data=data,
        options={CONF_ENABLE_EXPERIMENTAL: False, CONF_MUSIC_ASSISTANT_TOKEN: MA_TOKEN},
    )
    await async_setup_engine(hass, old_entry)

    assert old_entry.version == 2
    assert old_entry.data[CONF_MUSIC_ASSISTANT_TOKEN] == MA_TOKEN
    assert CONF_MUSIC_ASSISTANT_TOKEN not in old_entry.options
    assert await hass.config_entries.async_unload(old_entry.entry_id)


async def test_entry_from_a_newer_version_is_refused(
    hass: HomeAssistant,
    mock_music_assistant: MusicAssistantStub,
    config_entry: MockConfigEntry,
) -> None:
    """An entry written by a newer Engine release is not guessed at."""
    newer = MockConfigEntry(
        domain=DOMAIN, version=3, unique_id="default", data=dict(config_entry.data)
    )
    newer.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(newer.entry_id)
    assert newer.state is ConfigEntryState.MIGRATION_ERROR
