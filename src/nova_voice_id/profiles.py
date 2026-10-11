"""Enrolled voices on disk.

profiles/<person>.npz holds a person's embeddings, one per sample, with the
model name and each sample's id, time and source. recordings/<person>/ holds
the matching audio as <sample_id>.wav, so profiles can be rebuilt when the
model changes. Every file is mode 0600.

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
MAX_SAMPLES = 24
SAMPLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


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
    sources: tuple[str, ...]

    @property
    def updated(self) -> float:
        return max(self.created) if self.created else 0.0


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
    def __init__(self, data_dir: Path) -> None:
        self._profiles_dir = data_dir / "profiles"
        self._recordings_dir = data_dir / "recordings"
        self._profiles: dict[str, Profile] = {}

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
        source: str = "nova",
    ) -> int:
        """Save a sample to a person. Returns their sample count."""
        if not valid_person(person) or not SAMPLE_ID_RE.fullmatch(sample_id):
            raise ValueError("bad person or sample id")
        self._ensure_dirs()
        person_dir = self._recordings_dir / person
        person_dir.mkdir(mode=0o700, exist_ok=True)
        write_wav(person_dir / f"{sample_id}.wav", pcm)

        old = self._profiles.get(person)
        if old is not None and old.model == model:
            embeddings = np.vstack([old.embeddings, embedding[None, :]])
            sample_ids = (*old.sample_ids, sample_id)
            created = (*old.created, time.time())
            sources = (*old.sources, source)
        else:
            embeddings = embedding[None, :].astype(np.float32)
            sample_ids, created, sources = (sample_id,), (time.time(),), (source,)

        # Over the cap: drop the oldest samples and their recordings.
        while len(sample_ids) > MAX_SAMPLES:
            oldest = int(np.argmin(created))
            (person_dir / f"{sample_ids[oldest]}.wav").unlink(missing_ok=True)
            embeddings = np.delete(embeddings, oldest, axis=0)
            sample_ids = sample_ids[:oldest] + sample_ids[oldest + 1 :]
            created = created[:oldest] + created[oldest + 1 :]
            sources = sources[:oldest] + sources[oldest + 1 :]

        profile = Profile(person, model, embeddings, sample_ids, created, sources)
        self._write(profile)
        self._set(person, profile)
        return len(sample_ids)

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
            keep = []
            for index, sample_id in enumerate(profile.sample_ids):
                path = self.recording_path(profile.person, sample_id)
                try:
                    vector = embed(read_wav(path))
                except (OSError, ValueError, EOFError, wave.Error):
                    vector = None
                if vector is not None:
                    keep.append((index, vector))
            if not keep:
                _LOGGER.warning("A profile had no usable recordings and was emptied")
                new = Profile(profile.person, model, np.zeros((0, 0), np.float32), (), (), ())
            else:
                new = Profile(
                    profile.person,
                    model,
                    np.vstack([vector for _, vector in keep]).astype(np.float32),
                    tuple(profile.sample_ids[i] for i, _ in keep),
                    tuple(profile.created[i] for i, _ in keep),
                    tuple(profile.sources[i] for i, _ in keep),
                )
            self._write(new)
            self._set(profile.person, new)
            count += len(keep)
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
                sources=np.array(profile.sources, dtype=str),
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
        return Profile(person, model, embeddings, sample_ids, created, sources)
