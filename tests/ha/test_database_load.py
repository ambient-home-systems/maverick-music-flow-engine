"""Recorder and CPU load: MA bus events, entity attributes and storage writes (B-7)."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from conftest import (
    KITCHEN,
    KITCHEN_ID,
    FakeMusicAssistant,
    async_wait_for,
    engine_entity_id,
    engine_runtime,
)
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.json import json_bytes
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.maverick_music_flow.const import (
    EVENT_MUSIC_ASSISTANT,
    SIGNAL_ENGINE_UPDATED,
    STORAGE_KEY,
)
from custom_components.maverick_music_flow.entity_attributes import UNRECORDED_ATTRIBUTES
from custom_components.maverick_music_flow.runtime import ACTIVITY_SAVE_DELAY
from custom_components.maverick_music_flow.sensor import SENSORS

# The recorder drops the attributes of a state whose JSON is larger than this
# (homeassistant.components.recorder.db_schema.MAX_STATE_ATTRS_BYTES).
RECORDER_MAX_ATTRIBUTE_BYTES = 16384


def _ma_event(event: str, sequence: int, object_id: str = "q1") -> dict[str, Any]:
    """Return a message as MusicAssistantEventClient passes it to the runtime."""
    return {
        "kind": "event",
        "event": event,
        "object_id": object_id,
        "sequence": sequence,
        "occurred_at": "2026-09-29T12:00:00Z",
    }


def _listen(hass: HomeAssistant) -> list[Event]:
    events: list[Event] = []
    hass.bus.async_listen(EVENT_MUSIC_ASSISTANT, events.append)
    return events


async def _close_forward_window(hass: HomeAssistant) -> None:
    """Let open MA event forward windows close until no held event is left.

    Closing a window that held events forwards them and opens a new window.
    """
    runtime = engine_runtime(hass)
    for _ in range(5):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
        await hass.async_block_till_done()
        if runtime._ma_event_window_unsub is None:
            return
    raise AssertionError("the MA event forward window did not close")


async def test_progress_events_do_not_reach_the_bus(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, fake_ma: FakeMusicAssistant
) -> None:
    """Per-second progress events never go on the bus, where the recorder stores them."""
    events = _listen(hass)
    for event in ("queue_time_updated", "player_time_updated", "queue_time_updated"):
        await fake_ma.async_send_event(event, object_id=KITCHEN_ID)
    await fake_ma.async_send_event("player_updated", object_id=KITCHEN_ID)
    await async_wait_for(
        lambda: any(event.data.get("event") == "player_updated" for event in events),
        message="player_updated on the bus",
    )
    assert [event.data["event"] for event in events if event.data.get("kind") == "event"] == [
        "player_updated"
    ]


async def test_progress_events_do_not_refresh_entities(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """A progress event neither fires a bus event nor re-renders every entity."""
    runtime = engine_runtime(hass)
    await _close_forward_window(hass)
    events = _listen(hass)
    signals: list[None] = []
    loaded_entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_ENGINE_UPDATED, lambda: signals.append(None))
    )
    runtime._handle_music_assistant_message(_ma_event("queue_time_updated", 1))
    await hass.async_block_till_done()
    assert events == []
    assert signals == []


async def test_other_events_are_forwarded_at_most_once_per_window(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """A burst sends the first event at once and the latest of each kind when it ends."""
    runtime = engine_runtime(hass)
    await _close_forward_window(hass)
    events = _listen(hass)
    signals: list[None] = []
    loaded_entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_ENGINE_UPDATED, lambda: signals.append(None))
    )

    runtime._handle_music_assistant_message(_ma_event("queue_updated", 1))
    await hass.async_block_till_done()
    assert [event.data["sequence"] for event in events] == [1]
    assert len(signals) == 1

    runtime._handle_music_assistant_message(_ma_event("queue_updated", 2))
    runtime._handle_music_assistant_message(_ma_event("queue_updated", 3, "q2"))
    runtime._handle_music_assistant_message(_ma_event("queue_updated", 4))
    await hass.async_block_till_done()
    assert [event.data["sequence"] for event in events] == [1]
    assert len(signals) == 1

    await _close_forward_window(hass)
    # Sequence 2 was replaced by 4, the latest queue_updated for the same queue. (A
    # player_updated would do the same, but it also starts a 2-second players refresh
    # that async_block_till_done waits for, which would close the window.)
    assert [event.data["sequence"] for event in events] == [1, 3, 4]
    assert len(signals) == 2


async def test_forward_window_is_cancelled_on_unload(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Held events are dropped and no timer outlives the config entry."""
    runtime = engine_runtime(hass)
    await _close_forward_window(hass)
    runtime._handle_music_assistant_message(_ma_event("queue_updated", 1))
    runtime._handle_music_assistant_message(_ma_event("queue_updated", 2))
    assert runtime._ma_event_window_unsub is not None

    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()
    assert runtime._ma_event_window_unsub is None
    assert runtime._ma_event_pending == {}


async def test_connection_snapshot_is_built_once_per_signal(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Every entity rendering for one signal shares one required-connections snapshot."""
    runtime = engine_runtime(hass)
    builds: list[None] = []
    reads: list[None] = []
    build = runtime._build_required_connections_snapshot
    read = runtime.required_connections_snapshot

    def counting_build(all_players: list[dict[str, Any]] | None) -> dict[str, Any]:
        builds.append(None)
        return build(all_players)

    def counting_read(**kwargs: Any) -> dict[str, Any]:
        reads.append(None)
        return read(**kwargs)

    with (
        patch.object(runtime, "_build_required_connections_snapshot", counting_build),
        patch.object(runtime, "required_connections_snapshot", counting_read),
    ):
        await hass.async_block_till_done()
        builds.clear()
        reads.clear()

        async_dispatcher_send(hass, SIGNAL_ENGINE_UPDATED)
        assert len(builds) == 1
        # The sensors and binary sensors read it about 20 times.
        assert len(reads) >= 15

        # The next signal gets a new snapshot, even in the same event loop iteration.
        async_dispatcher_send(hass, SIGNAL_ENGINE_UPDATED)
        assert len(builds) == 2

        # Callers after this event loop iteration get a new snapshot too.
        await hass.async_block_till_done()
        runtime.required_connections_snapshot()
        assert len(builds) == 3

    connections = hass.states.get(
        engine_entity_id(hass, loaded_entry, "sensor", "required_connections")
    )
    assert connections.state == "healthy"


async def test_status_sensor_attributes_are_small_and_stable(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """The Status sensor stays under the recorder's limit and only changes with the data."""
    entity_id = engine_entity_id(hass, loaded_entry, "sensor", "status")
    state = hass.states.get(entity_id)
    attributes = dict(state.attributes)
    assert len(json_bytes(attributes)) < RECORDER_MAX_ATTRIBUTE_BYTES
    assert "generated_at" not in attributes
    assert "capabilities" not in attributes
    assert "generated_at" not in attributes["required_connections"]
    assert attributes["engine_version"]

    # What the recorder stores is a few stable fields.
    unrecorded = state.state_info["unrecorded_attributes"]
    assert unrecorded >= UNRECORDED_ATTRIBUTES
    recorded = {key: value for key, value in attributes.items() if key not in unrecorded}
    assert set(recorded) >= {"available", "version", "instance_id", "profile_id"}
    assert len(json_bytes(recorded)) < 1024

    # The card still gets the full context over the WebSocket API.
    context = engine_runtime(hass).context()
    assert context["capabilities"]
    assert context["generated_at"]


async def test_entities_are_not_rewritten_when_nothing_changed(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Without force_update or per-call timestamps, an unchanged entity is not written."""
    runtime = engine_runtime(hass)
    # Stop playback, so the playback statistics do not grow between two signals.
    kitchen = hass.states.get(KITCHEN)
    hass.states.async_set(KITCHEN, "paused", kitchen.attributes)
    await hass.async_block_till_done()
    async_dispatcher_send(hass, SIGNAL_ENGINE_UPDATED)
    await hass.async_block_till_done()
    keys = [
        ("sensor", "status"),
        ("sensor", "required_connections"),
        ("sensor", "required_connection_music_assistant"),
        ("sensor", "playback_today"),
        ("sensor", "playback_sessions_today"),
        ("sensor", "top_player_today"),
        ("sensor", "screensaver_recommendation"),
        ("binary_sensor", "required_connections_ok"),
        ("binary_sensor", "music_assistant_connected"),
    ]
    entity_ids = [engine_entity_id(hass, loaded_entry, domain, key) for domain, key in keys]

    before = {entity_id: hass.states.get(entity_id) for entity_id in entity_ids}

    # MA events (progress included) must not change the entities either.
    runtime._handle_music_assistant_message(_ma_event("queue_time_updated", 1))
    async_dispatcher_send(hass, SIGNAL_ENGINE_UPDATED)
    await hass.async_block_till_done()

    for entity_id in entity_ids:
        after = hass.states.get(entity_id)
        assert after.last_updated == before[entity_id].last_updated, entity_id


def test_no_sensor_forces_updates() -> None:
    """force_update would record a state on every signal even when nothing changed."""
    assert [description.key for description in SENSORS if description.force_update] == []


async def test_activity_records_use_a_delayed_save(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    """Activity records are batched into one delayed write instead of a write each."""
    runtime = engine_runtime(hass)
    await hass.async_block_till_done()
    store = runtime._store

    def stored_messages() -> list[str]:
        data = hass_storage.get(STORAGE_KEY, {}).get("data", {})
        return [item.get("message") for item in data.get("activity", [])]

    with (
        patch.object(store, "async_save", wraps=store.async_save) as save,
        patch.object(store, "async_delay_save", wraps=store.async_delay_save) as delay_save,
    ):
        for index in range(3):
            await runtime.async_record_activity("volume_rule_applied", f"Volume limited {index}")
        save.assert_not_called()
        assert delay_save.call_count == 1
        assert delay_save.call_args.args[1] == ACTIVITY_SAVE_DELAY
        assert "Volume limited 0" not in stored_messages()
        # The sensors still see the records at once.
        activity = hass.states.get(engine_entity_id(hass, loaded_entry, "sensor", "last_activity"))
        assert activity.state == "Volume limited 2"

        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=ACTIVITY_SAVE_DELAY + 1))
        await hass.async_block_till_done()
        assert stored_messages()[:3] == [f"Volume limited {index}" for index in (2, 1, 0)]

        # After the write, the next record schedules a new delayed write.
        await runtime.async_record_activity("volume_rule_applied", "Volume limited 3")
        assert delay_save.call_count == 2

        # A full write stores pending records too, and the next record schedules again.
        await runtime.async_save()
        assert stored_messages()[0] == "Volume limited 3"
        await runtime.async_record_activity("volume_rule_applied", "Volume limited 4")
        assert delay_save.call_count == 3


async def test_unload_writes_pending_activity(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    """Records still waiting for their delayed write are saved when the entry unloads."""
    runtime = engine_runtime(hass)
    await runtime.async_record_activity("volume_rule_applied", "Volume limited on unload")
    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()
    stored = json.dumps(hass_storage[STORAGE_KEY]["data"]["activity"])
    assert "Volume limited on unload" in stored
