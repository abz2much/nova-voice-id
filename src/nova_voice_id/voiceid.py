"""Voice ID: the speaker path and the stores behind it.

One VoiceId is shared by all connections. Each request gets its own analysis
in the speaker pool; nothing about one request is visible to another. Disk
work (profiles, recordings, scores.db) runs in one storage thread, so writes
happen in order without a lock and never slow the speaker pool.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import speaker
from .pending import PendingStore
from .profiles import ProfileStore
from .scores import ScoreLog
from .speaker import Analysis, Decision, Engine, Thresholds

_LOGGER = logging.getLogger(__name__)

# How long a transcript may wait for the speaker result.
RESULT_WAIT_S = 0.4


@dataclass
class Outcome:
    """The speaker result for one request, before it is sent."""

    status: str
    analysis: Analysis | None = None
    decision: Decision | None = None
    done_at: float = 0.0


class VoiceId:
    def __init__(
        self,
        data_dir: Path,
        engine: Engine,
        thresholds: Thresholds,
        pending: PendingStore | None = None,
        result_wait_s: float = RESULT_WAIT_S,
    ) -> None:
        self.engine = engine
        self.thresholds = thresholds
        self.result_wait_s = result_wait_s
        self.profiles = ProfileStore(data_dir)
        self.scores = ScoreLog(data_dir / "scores.db")
        self.pending = pending or PendingStore()
        self._storage = ThreadPoolExecutor(max_workers=1, thread_name_prefix="storage")

    @property
    def model(self) -> str:
        return self.engine.model

    async def storage(self, func: Callable[..., Any], *args: Any) -> Any:
        """Run disk work in the storage thread."""
        return await asyncio.get_running_loop().run_in_executor(self._storage, func, *args)

    async def start(self) -> None:
        """Load the models and profiles, and rebuild any made with another model."""
        await asyncio.to_thread(self.engine.warm_up)
        await self.storage(self.profiles.load)
        stale = [p for p in self.profiles.all() if p.model != self.model]
        if stale:
            count = await self.storage(self.profiles.reembed, self.model, self._embed_blocking)
            _LOGGER.info(
                "Model is now %s. Re-embedded %d samples from recordings for %d profiles.",
                self.model,
                count,
                len(stale),
            )
        await self.storage(self.scores.purge)

    def _embed_blocking(self, pcm: bytes):
        # Called in the storage thread during start-up. The model runs in the
        # speaker pool, where its per-thread models live.
        return self.engine.submit(self.engine.embed_recording, pcm).result()

    async def analyse(self, pcm: bytes) -> Outcome:
        """Trim, VAD, embed and score one request's audio."""
        queued = time.monotonic()
        timing: dict[str, float] = {}

        def work() -> Analysis:
            timing["start"] = time.monotonic()
            return self.engine.analyse(pcm, self.thresholds)

        analysis = await self.engine.run(work)
        _LOGGER.debug(
            "Speaker step: waited %d ms for a worker, ran %d ms",
            (timing["start"] - queued) * 1000,
            (time.monotonic() - timing["start"]) * 1000,
        )
        if analysis.embedding is None:
            return Outcome(speaker.SHORT, analysis, done_at=time.monotonic())
        decision = speaker.decide(
            analysis.embedding, analysis.speech_s, self.profiles.snapshot(), self.thresholds
        )
        return Outcome(decision.status, analysis, decision, time.monotonic())

    async def result_for(
        self, task: asyncio.Task[Outcome] | None, started: float
    ) -> dict[str, Any]:
        """The result block, waiting at most result_wait_s for the speaker task.

        `started` is when the speaker step began, for the ms field.
        """
        if task is None:
            return self._record(speaker.ERROR, started, None)
        try:
            outcome = await asyncio.wait_for(asyncio.shield(task), self.result_wait_s)
        except TimeoutError:
            task.add_done_callback(_ignore_result)
            return self._record(speaker.TIMEOUT, started, None)
        except Exception as err:  # noqa: BLE001 - any speaker failure becomes status error
            _LOGGER.warning("Speaker step failed: %s", err.__class__.__name__)
            return self._record(speaker.ERROR, started, None)
        return self._record(outcome.status, started, outcome)

    def _record(self, status: str, started: float, outcome: Outcome | None) -> dict[str, Any]:
        """Keep the sample, log the score row, and build the result block."""
        # Time the speaker step took, or how long it ran before being given up.
        ms = round(((outcome.done_at if outcome else time.monotonic()) - started) * 1000)
        analysis = outcome.analysis if outcome else None
        decision = outcome.decision if outcome else None

        sample_id = None
        if analysis is not None and analysis.embedding is not None:
            candidates = frozenset(
                name for name in (decision.best, decision.second) if decision and name
            )
            sample_id = self.pending.add(
                analysis.embedding, analysis.trimmed_pcm, self.model, candidates
            )

        speech_s = analysis.speech_s if analysis is not None else None
        row = {
            "time": time.time(),
            "status": status,
            "person": decision.best if decision else None,
            "score": decision.score if decision else None,
            "second": decision.second if decision else None,
            "second_score": decision.second_score if decision else None,
            "margin": decision.margin if decision else None,
            "speech_s": speech_s,
            "model": self.model,
        }
        self._storage.submit(self._add_row, row)
        return speaker.result_block(status, self.model, ms, decision, speech_s, sample_id)

    def _add_row(self, row: dict[str, Any]) -> None:
        try:
            self.scores.add(row)
        except Exception as err:  # noqa: BLE001 - the score log must never break a request
            _LOGGER.warning("Could not write the score log: %s", err.__class__.__name__)

    def close(self) -> None:
        self.engine.close()
        self._storage.submit(self.scores.close)
        self._storage.shutdown(wait=True)


def _ignore_result(task: asyncio.Task) -> None:
    # The speaker step finished after its result was given up. Read any
    # exception so asyncio doesn't log it.
    if not task.cancelled():
        task.exception()
