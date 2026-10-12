"""Pending samples: recent embeddings and audio, in memory only.

Nova can commit one to a person within 10 minutes. Nothing here is written to
disk. Only the event loop thread touches this store.
"""

import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

MAX_AGE_S = 600.0
MAX_ITEMS = 50


@dataclass(frozen=True)
class PendingSample:
    embedding: np.ndarray
    pcm: bytes
    model: str
    created: float
    # Who the sample scored best and second best against, if anyone. Used to
    # drop it when one of them is deleted.
    candidates: frozenset[str] = field(default_factory=frozenset)
    # The sample's own result, so a learned commit can check it was known
    # for the same person. `person` is set only when the status was known.
    status: str = ""
    person: str | None = None


class PendingStore:
    def __init__(
        self,
        max_age: float = MAX_AGE_S,
        max_items: int = MAX_ITEMS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_age = max_age
        self._max_items = max_items
        self._clock = clock
        self._items: OrderedDict[str, PendingSample] = OrderedDict()

    def __len__(self) -> int:
        self._expire()
        return len(self._items)

    def add(
        self,
        embedding: np.ndarray,
        pcm: bytes,
        model: str,
        candidates: frozenset[str],
        status: str = "",
        person: str | None = None,
    ) -> str:
        self._expire()
        sample_id = secrets.token_urlsafe(12)
        self._items[sample_id] = PendingSample(
            embedding, pcm, model, self._clock(), candidates, status, person
        )
        while len(self._items) > self._max_items:
            self._items.popitem(last=False)  # oldest first
        return sample_id

    def get(self, sample_id: str) -> PendingSample | None:
        self._expire()
        return self._items.get(sample_id)

    def remove(self, sample_id: str) -> None:
        self._items.pop(sample_id, None)

    def remove_person(self, person: str) -> None:
        for sample_id in [k for k, v in self._items.items() if person in v.candidates]:
            del self._items[sample_id]

    def clear(self) -> None:
        self._items.clear()

    def _expire(self) -> None:
        cutoff = self._clock() - self._max_age
        while self._items:
            sample_id, sample = next(iter(self._items.items()))
            if sample.created >= cutoff:
                break
            del self._items[sample_id]
