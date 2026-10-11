"""Management events from Nova, over the same Wyoming connection.

Every event needs the token. Replies are <event type>-result, or an error
event with a code. The token is never logged.
"""

import logging
import math
from typing import Any

from wyoming.error import Error
from wyoming.event import Event

from . import auth
from .profiles import SAMPLE_ID_RE, valid_person
from .voiceid import VoiceId

_LOGGER = logging.getLogger(__name__)

PREFIX = "nvid-"
ENROL_COMMIT = "nvid-enrol-commit"
PROFILE_DELETE = "nvid-profile-delete"
PROFILE_LIST = "nvid-profile-list"
STATS = "nvid-stats"

UNAUTHORISED = "unauthorised"
BAD_REQUEST = "bad_request"
NOT_FOUND = "not_found"
FAILED = "failed"

MAX_STATS_DAYS = 30


def is_management(event: Event) -> bool:
    return event.type.startswith(PREFIX)


def _error(text: str, code: str) -> Event:
    return Error(text=text, code=code).event()


def _reply(event_type: str, data: dict[str, Any]) -> Event:
    return Event(type=f"{event_type}-result", data=data)


class Management:
    def __init__(self, voiceid: VoiceId, token: str) -> None:
        self._voiceid = voiceid
        self._token = token

    async def handle(self, event: Event) -> Event:
        data = event.data or {}
        if not auth.matches(data.get("token"), self._token):
            _LOGGER.warning("Rejected %s: missing or wrong token", event.type)
            return _error("Missing or wrong token", UNAUTHORISED)

        try:
            if event.type == ENROL_COMMIT:
                return await self._enrol_commit(data)
            if event.type == PROFILE_DELETE:
                return await self._profile_delete(data)
            if event.type == PROFILE_LIST:
                return await self._profile_list()
            if event.type == STATS:
                return await self._stats(data)
        except OSError as err:
            _LOGGER.warning("%s failed: %s", event.type, err.__class__.__name__)
            return _error("Storage failed", FAILED)

        return _error("Unknown event type", BAD_REQUEST)

    async def _enrol_commit(self, data: dict[str, Any]) -> Event:
        person = data.get("person")
        sample_id = data.get("sample_id")
        if not valid_person(person):
            _LOGGER.warning("Rejected %s: bad person id", ENROL_COMMIT)
            return _error("person must match ^person\\.[a-z0-9_]+$", BAD_REQUEST)
        if not isinstance(sample_id, str) or not SAMPLE_ID_RE.fullmatch(sample_id):
            return _error("Bad sample_id", BAD_REQUEST)

        voiceid = self._voiceid
        sample = voiceid.pending.get(sample_id)
        if sample is None:
            return _error("No pending sample with that id (it may have expired)", NOT_FOUND)
        if sample.model != voiceid.model:
            voiceid.pending.remove(sample_id)
            return _error("That sample was made with another model", NOT_FOUND)

        count = await voiceid.storage(
            voiceid.profiles.commit, person, sample_id, sample.embedding, sample.pcm, sample.model
        )
        voiceid.pending.remove(sample_id)
        _LOGGER.info("Saved a voice sample (%d samples for that person)", count)
        return _reply(ENROL_COMMIT, {"person": person, "samples": count})

    async def _profile_delete(self, data: dict[str, Any]) -> Event:
        voiceid = self._voiceid
        if data.get("all") is True:
            people = await voiceid.storage(voiceid.profiles.delete_all)
            await voiceid.storage(voiceid.scores.delete_all)
            voiceid.pending.clear()
            _LOGGER.info("Deleted all voice profiles (%d)", len(people))
            return _reply(PROFILE_DELETE, {"deleted": people, "all": True})

        person = data.get("person")
        if not valid_person(person):
            _LOGGER.warning("Rejected %s: bad person id", PROFILE_DELETE)
            return _error("Give person (^person\\.[a-z0-9_]+$) or all: true", BAD_REQUEST)

        found = await voiceid.storage(voiceid.profiles.delete, person)
        await voiceid.storage(voiceid.scores.delete_person, person)
        voiceid.pending.remove_person(person)
        _LOGGER.info("Deleted a voice profile")
        return _reply(PROFILE_DELETE, {"deleted": [person] if found else [], "all": False})

    async def _profile_list(self) -> Event:
        voiceid = self._voiceid
        profiles = [
            {"person": p.person, "samples": len(p.sample_ids), "updated": round(p.updated, 3)}
            for p in voiceid.profiles.all()
        ]
        return _reply(PROFILE_LIST, {"profiles": profiles, "model": voiceid.model})

    async def _stats(self, data: dict[str, Any]) -> Event:
        days = data.get("days", 7)
        if (
            isinstance(days, bool)
            or not isinstance(days, (int, float))
            or not math.isfinite(days)
            or days <= 0
        ):
            return _error("days must be a positive number", BAD_REQUEST)
        days = min(float(days), MAX_STATS_DAYS)
        rows, truncated = await self._voiceid.storage(self._voiceid.scores.rows, days)
        return _reply(STATS, {"days": days, "rows": rows, "truncated": truncated})
