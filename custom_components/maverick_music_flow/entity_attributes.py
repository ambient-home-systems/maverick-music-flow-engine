"""State attributes of the Engine's sensor and binary sensor entities.

This module has no Home Assistant imports so the rules can be tested directly.

Every attribute change is a new row in Home Assistant's recorder, so the entities keep
their attributes stable. Timestamps that are simply the time of the call
(``generated_at``, and the playback statistics' ``last_updated_at``), the Music
Assistant event counter (which counts every progress event) and the static
``capabilities`` map are dropped; the card reads all of them from the WebSocket
commands (``get_context``, ``playback_stats/get``). Large or frequently changing values
are still shown on the entity but listed in :data:`UNRECORDED_ATTRIBUTES`, so the
recorder does not store them.
"""

from __future__ import annotations

from typing import Any

# Dropped at every level of an entity's attributes. The timestamps are the time the value
# was built, and the Music Assistant connection's event_count and last_event_at change
# with every Music Assistant event, so each would change the entity on every update.
VOLATILE_ATTRIBUTES = frozenset({"generated_at", "last_updated_at", "event_count", "last_event_at"})

# Dropped from the Status sensor, whose attributes are otherwise the card context.
STATUS_DROPPED_ATTRIBUTES = frozenset({"capabilities"})

# Attributes the recorder does not store. Home Assistant only supports one set per entity
# class, so this is the union for every sensor and binary sensor description.
UNRECORDED_ATTRIBUTES = frozenset(
    {
        # Status sensor (the card context).
        "entries",
        "frontend",
        "interface_preferences",
        "media_cache",
        "music_assistant",
        "required_connections",
        # Required connections sensor and binary sensor (the whole snapshot).
        "command_bridge",
        "connections",
        "library_provider",
        "queue_provider",
        "realtime_events",
        "search_provider",
        # One required connection.
        "missing_services",
        "players",
        "probe",
        "required_services",
        "services",
        # Schedule sensors: the orchestration status changes on every 30-second tick.
        "last_schedule_check",
        "scheduler_status",
    }
)


def stable_attributes(value: Any) -> Any:
    """Return a copy of value without VOLATILE_ATTRIBUTES at any level."""
    if isinstance(value, dict):
        return {
            key: stable_attributes(item)
            for key, item in value.items()
            if key not in VOLATILE_ATTRIBUTES
        }
    if isinstance(value, list):
        return [stable_attributes(item) for item in value]
    return value


def status_attributes(context: dict[str, Any]) -> dict[str, Any]:
    """Return the Status sensor's attributes for a card context."""
    return {key: value for key, value in context.items() if key not in STATUS_DROPPED_ATTRIBUTES}
