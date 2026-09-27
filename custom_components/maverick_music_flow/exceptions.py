"""Exceptions for Maverick Music Flow Engine."""

from __future__ import annotations


class HomeiiFlowEngineError(Exception):
    """Base Maverick Music Flow Engine error."""


class HomeiiFlowServiceUnavailable(HomeiiFlowEngineError):
    """Raised when a requested Home Assistant service is unavailable."""


class HomeiiFlowPlaybackUnconfirmed(HomeiiFlowServiceUnavailable):
    """Raised when Music Assistant accepted a play command but playback was not confirmed.

    The command reached the queue, so callers must not re-send it. ``result`` holds the
    play_media result so a caller can keep checking without replaying the media.
    """

    def __init__(self, message: str, result: dict) -> None:
        """Store the play_media result alongside the message."""
        super().__init__(message)
        self.result = result
