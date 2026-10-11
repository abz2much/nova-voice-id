"""The speaker step.

Phase 1a does no speaker recognition. This module holds the parts a later
phase fills in: the audio kept for one request, and the result that is added
to the transcript. Nothing here is shared between requests.
"""

from typing import Any

from wyoming.audio import AudioChunk
from wyoming.event import Event

RESULT_VERSION = 1

# About 60 s of 16 kHz, 16 bit, mono audio. Audio past this is still sent to
# the upstream, it is just not kept.
MAX_KEPT_BYTES = 1_920_000


class RequestAudio:
    """The audio of one request, kept in memory until the request ends."""

    def __init__(self) -> None:
        self._audio = bytearray()
        self.seconds = 0.0

    def add(self, event: Event) -> None:
        """Keep a copy of an audio-chunk event's audio."""
        chunk = AudioChunk.from_event(event)
        self.seconds += chunk.seconds

        room = MAX_KEPT_BYTES - len(self._audio)
        if room > 0:
            self._audio.extend(chunk.audio[:room])

    def clear(self) -> None:
        """Drop the kept audio."""
        self._audio = bytearray()


def identify(audio: RequestAudio) -> dict[str, Any]:
    """Work out who is speaking. Off in this phase."""
    return {"v": RESULT_VERSION, "status": "off"}
