"""Enrolled voices on disk.

profiles/<person>.npz holds a person's embeddings, one per sample, with the
model name and each sample's id, time and kind. recordings/<person>/ holds
the matching audio as <sample_id>.wav, so profiles can be rebuilt when the
model changes. Every file is mode 0600.

A sample is one of two kinds. "enrolled" samples were recorded by the user.
"learned" samples were added from everyday use. Each kind has its own cap,
so learning can never push out or replace the user's own recording, and
learned samples can be removed on their own.

Disk work runs in a worker thread. The in-memory copy is replaced, never
changed in place, so a request scoring against it always sees a whole profile.
"""

import logging
import os
import re
import shutil
import tempfile
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .speaker import SAMPLE_RATE, SAMPLE_WIDTH

_LOGGER = logging.getLogger(__name__)

PERSON_RE = re.compile(r"^person\.[a-z0-9_]+$")
PERSON_MAX_LEN = 128
SAMPLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


ENROLLED = "enrolled"
LEARNED = "learned"
KINDS = (ENROLLED, LEARNED)
MAX_PER_KIND = {ENROLLED: 24, LEARNED: 16}

# A learned sample needs this many enrolled samples first, and at most this
# many learned samples may be saved per person in DAY_S seconds.
LEARNED_NEEDS_ENROLLED = 3
LEARNED_PER_DAY = 3
DAY_S = 24 * 3600

# Sources written by 0.2.0. Its samples were all recorded by the user.
LEGACY_KINDS = {"nova": ENROLLED}


class Refused(Exception):
    """A learned sample that may not be saved. Nothing was changed."""


def valid_person(person: object) -> bool:
    return (
        isinstance(person, str)
        and len(person) <= PERSON_MAX_LEN
        and PERSON_RE.fullmatch(person) is not None
    )


@dataclass(frozen=True)
class Profile:
    person: str
    model: str
    embeddings: np.ndarray  # samples x dim, unit rows
    sample_ids: tuple[str, ...]
    created: tuple[float, ...]
    kinds: tuple[str, ...]

    @property
    def updated(self) -> float:
        return max(self.created) if self.created else 0.0

    def count(self, kind: str) -> int:
        return sum(1 for k in self.kinds if k == kind)

    def learned_since(self, since: float) -> int:
        """Learned samples saved after `since`."""
        return sum(
            1 for k, t in zip(self.kinds, self.created, strict=True) if k == LEARNED and t > since
        )

    def without(self, index: int) -> "Profile":
        """This profile with one sample taken out."""
        return Profile(
            self.person,
            self.model,
            np.delete(self.embeddings, index, axis=0),
            self.sample_ids[:index] + self.sample_ids[index + 1 :],
            self.created[:index] + self.created[index + 1 :],
            self.kinds[:index] + self.kinds[index + 1 :],
        )

    def keep(self, indexes: list[int], embeddings: np.ndarray | None = None) -> "Profile":
        """This profile with only some samples, optionally with new embeddings."""
        if embeddings is None:
            embeddings = self.embeddings[indexes] if indexes else np.zeros((0, 0), np.float32)
        return Profile(
            self.person,
            self.model,
            embeddings.astype(np.float32),
            tuple(self.sample_ids[i] for i in indexes),
            tuple(self.created[i] for i in indexes),
            tuple(self.kinds[i] for i in indexes),
        )


def _write_private(path: Path, write: Callable) -> None:
    """Write a file atomically with mode 0600."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_wav(path: Path, pcm: bytes) -> None:
    def write(handle):
        with wave.open(handle, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(SAMPLE_WIDTH)
            out.setframerate(SAMPLE_RATE)
            out.writeframes(pcm)

    _write_private(path, write)


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (
            1,
            SAMPLE_WIDTH,
            SAMPLE_RATE,
        ):
            raise ValueError("not 16 kHz, 16 bit, mono")
        return wav.readframes(wav.getnframes())


class ProfileStore:
    def __init__(self, data_dir: Path, clock: Callable[[], float] = time.time) -> None:
        self._profiles_dir = data_dir / "profiles"
        self._recordings_dir = data_dir / "recordings"
        self._profiles: dict[str, Profile] = {}
        self._clock = clock  # wall clock: saved times must survive a restart

    # Reading -----------------------------------------------------------

    def snapshot(self) -> dict[str, np.ndarray]:
        """Each person's embeddings. Safe to read from any thread."""
        profiles = self._profiles
        return {person: profile.embeddings for person, profile in profiles.items()}

    def get(self, person: str) -> Profile | None:
        return self._profiles.get(person)

    def all(self) -> list[Profile]:
        return sorted(self._profiles.values(), key=lambda p: p.person)

    def recording_path(self, person: str, sample_id: str) -> Path:
        return self._recordings_dir / person / f"{sample_id}.wav"

    def profile_path(self, person: str) -> Path:
        return self._profiles_dir / f"{person}.npz"

    # Disk work (call from a worker thread) -----------------------------

    def load(self) -> None:
        self._ensure_dirs()
        profiles = {}
        for path in sorted(self._profiles_dir.glob("person.*.npz")):
            person = path.name[: -len(".npz")]
            if not valid_person(person):
                continue
            try:
                profiles[person] = self._read(person, path)
            except (OSError, ValueError, KeyError) as err:
                _LOGGER.warning("Skipped an unreadable profile: %s", err.__class__.__name__)
        self._profiles = profiles

    def commit(
        self,
        person: str,
        sample_id: str,
        embedding: np.ndarray,
        pcm: bytes,
        model: str,
        kind: str = ENROLLED,
    ) -> Profile:
        """Save a sample to a person. Returns their updated profile.

        Over a kind's cap, the oldest sample of that same kind is replaced, so
        a learned sample never removes an enrolled one. A learned sample is
        refused (raising Refused, with nothing changed) unless the person has
        enough enrolled samples and hasn't had too many learned ones today.
        """
        if not valid_person(person) or not SAMPLE_ID_RE.fullmatch(sample_id):
            raise ValueError("bad person or sample id")
        if kind not in KINDS:
            raise ValueError("bad kind")

        now = self._clock()
        old = self._profiles.get(person)
        if old is not None and old.model != model:
            old = None  # can't mix models; only happens if start-up re-embed failed

        if kind == LEARNED:
            if old is None or old.count(ENROLLED) < LEARNED_NEEDS_ENROLLED:
                raise Refused(f"needs at least {LEARNED_NEEDS_ENROLLED} enrolled samples")
            if old.learned_since(now - DAY_S) >= LEARNED_PER_DAY:
                raise Refused(f"already {LEARNED_PER_DAY} learned samples in 24 hours")

        self._ensure_dirs()
        person_dir = self._recordings_dir / person
        person_dir.mkdir(mode=0o700, exist_ok=True)
        write_wav(person_dir / f"{sample_id}.wav", pcm)

        if old is not None:
            profile = Profile(
                person,
                model,
                np.vstack([old.embeddings, embedding[None, :]]).astype(np.float32),
                (*old.sample_ids, sample_id),
                (*old.created, now),
                (*old.kinds, kind),
            )
        else:
            profile = Profile(
                person, model, embedding[None, :].astype(np.float32), (sample_id,), (now,), (kind,)
            )

        # Over this kind's cap: replace its oldest sample of the same kind.
        while profile.count(kind) > MAX_PER_KIND[kind]:
            same = [i for i, k in enumerate(profile.kinds) if k == kind]
            oldest = min(same, key=lambda i: profile.created[i])
            (person_dir / f"{profile.sample_ids[oldest]}.wav").unlink(missing_ok=True)
            profile = profile.without(oldest)

        self._write(profile)
        self._set(person, profile)
        return profile

    def delete_learned(self, person: str) -> int:
        """Remove a person's learned samples and their recordings, keeping the
        profile and the enrolled samples. Returns how many were removed."""
        if not valid_person(person):
            raise ValueError("bad person")
        profile = self._profiles.get(person)
        if profile is None:
            return 0
        learned = [i for i, k in enumerate(profile.kinds) if k == LEARNED]
        if not learned:
            return 0
        for i in learned:
            self.recording_path(person, profile.sample_ids[i]).unlink(missing_ok=True)
        kept = profile.keep([i for i, k in enumerate(profile.kinds) if k != LEARNED])
        self._write(kept)
        self._set(person, kept)
        return len(learned)

    def delete_learned_all(self) -> dict[str, int]:
        """Remove everyone's learned samples. Returns how many per person."""
        removed = {}
        for profile in self.all():
            count = self.delete_learned(profile.person)
            if count:
                removed[profile.person] = count
        return removed

    def delete(self, person: str) -> bool:
        """Remove a person's profile and recordings. True if anything was there."""
        if not valid_person(person):
            raise ValueError("bad person")
        found = False
        path = self.profile_path(person)
        if path.exists():
            path.unlink()
            found = True
        person_dir = self._recordings_dir / person
        if person_dir.exists():
            shutil.rmtree(person_dir)
            found = True
        self._set(person, None)
        return found

    def delete_all(self) -> list[str]:
        people = sorted(
            {p.name[: -len(".npz")] for p in self._profiles_dir.glob("person.*.npz")}
            | {p.name for p in self._recordings_dir.glob("person.*") if p.is_dir()}
            | set(self._profiles)
        )
        for person in people:
            if valid_person(person):
                self.delete(person)
        return people

    def reembed(self, model: str, embed: Callable[[bytes], np.ndarray | None]) -> int:
        """Rebuild every profile made with another model from its recordings.

        Returns how many samples were re-embedded.
        """
        count = 0
        for profile in list(self._profiles.values()):
            if profile.model == model:
                continue
            indexes, vectors = [], []
            for index, sample_id in enumerate(profile.sample_ids):
                path = self.recording_path(profile.person, sample_id)
                try:
                    vector = embed(read_wav(path))
                except (OSError, ValueError, EOFError, wave.Error):
                    vector = None
                if vector is not None:
                    indexes.append(index)
                    vectors.append(vector)
            if not vectors:
                _LOGGER.warning("A profile had no usable recordings and was emptied")
            # Each sample keeps its id, time and kind; only the embedding changes.
            new = profile.keep(indexes, np.vstack(vectors) if vectors else None)
            new = Profile(new.person, model, new.embeddings, new.sample_ids, new.created, new.kinds)
            self._write(new)
            self._set(profile.person, new)
            count += len(vectors)
        return count

    # Helpers -----------------------------------------------------------

    def _ensure_dirs(self) -> None:
        for directory in (self._profiles_dir, self._recordings_dir):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _set(self, person: str, profile: Profile | None) -> None:
        profiles = dict(self._profiles)
        if profile is None or len(profile.sample_ids) == 0:
            profiles.pop(person, None)
        else:
            profiles[person] = profile
        self._profiles = profiles  # one assignment, so readers see old or new

    def _write(self, profile: Profile) -> None:
        def write(handle):
            np.savez(
                handle,
                embeddings=profile.embeddings.astype(np.float32),
                sample_ids=np.array(profile.sample_ids, dtype=str),
                created=np.array(profile.created, dtype=np.float64),
                sources=np.array(profile.kinds, dtype=str),
                model=np.array(profile.model),
            )

        _write_private(self.profile_path(profile.person), write)

    def _read(self, person: str, path: Path) -> Profile:
        with np.load(path, allow_pickle=False) as data:
            embeddings = data["embeddings"].astype(np.float32)
            sample_ids = tuple(str(s) for s in data["sample_ids"])
            created = tuple(float(c) for c in data["created"])
            sources = tuple(str(s) for s in data["sources"])
            model = str(data["model"])
        if not (len(embeddings) == len(sample_ids) == len(created) == len(sources)):
            raise ValueError("profile arrays differ in length")
        # 0.2.0 wrote "nova" for every sample: those are enrolled. The file is
        # only rewritten with the new name the next time it changes.
        kinds = tuple(LEGACY_KINDS.get(source, source) for source in sources)
        if any(kind not in KINDS for kind in kinds):
            raise ValueError("unknown sample kind")
        return Profile(person, model, embeddings, sample_ids, created, kinds)
