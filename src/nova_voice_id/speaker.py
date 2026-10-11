"""Speaker recognition for one request.

The audio of a request is kept in memory. On audio-stop it is trimmed, run
through voice activity detection, embedded once, and scored against the
enrolled profiles. Models live in two worker threads, one set per thread, so
two requests never wait on each other.
"""

import asyncio
import math
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from wyoming.audio import AudioChunk
from wyoming.event import Event

RESULT_VERSION = 1
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
WORKERS = 2
VAD_WINDOW = 512
VAD_FILE = "silero_vad.onnx"

# Speaker models in the image. The first is the default.
MODELS = (
    "wespeaker_en_voxceleb_resnet34_LM",
    "wespeaker_en_voxceleb_CAM++_LM",
    "nemo_en_titanet_small",
)

# About 30 s of 16 kHz, 16 bit, mono audio. Audio past this is still sent to
# the upstream, it is just not kept.
MAX_KEPT_BYTES = 30 * SAMPLE_RATE * SAMPLE_WIDTH

# Statuses
KNOWN = "known"
NOT_SURE = "not_sure"
UNKNOWN = "unknown"
SHORT = "short"
NO_PROFILES = "no_profiles"
ERROR = "error"
TIMEOUT = "timeout"

# Score is the mean of each person's best few sample similarities.
TOP_SAMPLES = 3


class RequestAudio:
    """The audio of one request, kept in memory until the request ends."""

    def __init__(self) -> None:
        self._audio = bytearray()
        self.seconds = 0.0
        self.usable = True

    def add(self, event: Event) -> None:
        """Keep a copy of an audio-chunk event's audio."""
        chunk = AudioChunk.from_event(event)
        self.seconds += chunk.seconds
        if (chunk.rate, chunk.width, chunk.channels) != (SAMPLE_RATE, SAMPLE_WIDTH, 1):
            # Only 16 kHz, 16 bit, mono is analysed. Other audio still goes up.
            self.usable = False
            return

        room = MAX_KEPT_BYTES - len(self._audio)
        if room > 0:
            self._audio.extend(chunk.audio[:room])

    def pcm(self) -> bytes:
        return bytes(self._audio)

    def clear(self) -> None:
        self._audio = bytearray()


@dataclass(frozen=True)
class Thresholds:
    t_known: float = 0.55
    t_maybe: float = 0.40
    m_gap: float = 0.10
    min_speech_s: float = 0.8
    sure_speech_s: float = 1.2
    trim_start_ms: int = 300


@dataclass(frozen=True)
class Analysis:
    """What the worker thread found in one request's audio."""

    trimmed_pcm: bytes
    speech_s: float
    embedding: np.ndarray | None  # unit length, or None when too short


@dataclass(frozen=True)
class Decision:
    status: str
    best: str | None = None
    score: float | None = None
    second: str | None = None
    second_score: float | None = None
    margin: float | None = None


def pcm_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def unit(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if not math.isfinite(norm) or norm == 0.0:
        raise ValueError("embedding has no length")
    return (vector / norm).astype(np.float32)


# onnxruntime fails if two threads create sessions at the same moment, so
# models are created one thread at a time. This only happens at start-up,
# never while a request runs.
_CREATE_LOCK = threading.Lock()


class Models:
    """The embedding and VAD models of one worker thread."""

    def __init__(self, models_dir: Path, model: str) -> None:
        with _CREATE_LOCK:
            self._create(models_dir, model)

    def _create(self, models_dir: Path, model: str) -> None:
        import sherpa_onnx  # Loaded here so tests without models can import this module.

        self._sherpa = sherpa_onnx
        self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(models_dir / f"{model}.onnx"), num_threads=1
            )
        )
        self.vad = sherpa_onnx.VoiceActivityDetector(
            sherpa_onnx.VadModelConfig(
                silero_vad=sherpa_onnx.SileroVadModelConfig(model=str(models_dir / VAD_FILE)),
                sample_rate=SAMPLE_RATE,
                num_threads=1,
            ),
            buffer_size_in_seconds=60,
        )

    def voiced(self, samples: np.ndarray) -> np.ndarray:
        """The voiced parts of the audio, joined together."""
        self.vad.reset()
        for start in range(0, len(samples), VAD_WINDOW):
            self.vad.accept_waveform(samples[start : start + VAD_WINDOW])
        self.vad.flush()
        segments = []
        while not self.vad.empty():
            segments.append(np.asarray(self.vad.front.samples, dtype=np.float32))
            self.vad.pop()
        self.vad.reset()
        if not segments:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(segments)

    def embed(self, samples: np.ndarray) -> np.ndarray:
        stream = self.extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        stream.input_finished()
        if not self.extractor.is_ready(stream):
            raise ValueError("not enough audio for an embedding")
        return unit(np.asarray(self.extractor.compute(stream), dtype=np.float32))


class Engine:
    """Runs the models in a small pool of worker threads."""

    def __init__(self, models_dir: Path, model: str, workers: int = WORKERS) -> None:
        self.model = model
        self._models_dir = models_dir
        self._workers = workers
        self._local = threading.local()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="speaker")

    def warm_up(self) -> None:
        """Load the models in every worker thread now, not on the first request."""
        barrier = threading.Barrier(self._workers)

        def load() -> None:
            models = self._models()
            # One pass through each model, so the first real request isn't the
            # slow, cold one.
            warm = np.random.default_rng(0).standard_normal(SAMPLE_RATE).astype(np.float32) * 0.01
            models.voiced(warm)
            models.embed(warm)
            barrier.wait(timeout=60)  # holds each thread so every one gets a task

        for future in [self._pool.submit(load) for _ in range(self._workers)]:
            future.result()

    def _models(self) -> Models:
        models = getattr(self._local, "models", None)
        if models is None:
            models = Models(self._models_dir, self.model)
            self._local.models = models
        return models

    def analyse(self, pcm: bytes, thresholds: Thresholds) -> Analysis:
        """Trim, find the speech, and embed it once. Runs in a worker thread."""
        trim = int(thresholds.trim_start_ms * SAMPLE_RATE / 1000) * SAMPLE_WIDTH
        trimmed = pcm[trim:]
        models = self._models()
        voiced = models.voiced(pcm_to_float(trimmed))
        speech_s = len(voiced) / SAMPLE_RATE
        if speech_s < thresholds.min_speech_s:
            return Analysis(trimmed, speech_s, None)
        return Analysis(trimmed, speech_s, models.embed(voiced))

    def embed_recording(self, pcm: bytes) -> np.ndarray | None:
        """Embed a saved recording (already trimmed). None if it has no speech."""
        models = self._models()
        voiced = models.voiced(pcm_to_float(pcm))
        if len(voiced) == 0:
            return None
        return models.embed(voiced)

    def submit(self, func: Callable[..., Any], *args: Any) -> Future:
        return self._pool.submit(func, *args)

    async def run(self, func: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._pool, func, *args)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def person_scores(embedding: np.ndarray, profiles: Mapping[str, np.ndarray]) -> list[tuple]:
    """Each person's score, best first: the mean of their top 3 cosine similarities."""
    scores = []
    for person, matrix in profiles.items():
        if len(matrix) == 0:
            continue
        similarities = np.sort(matrix @ embedding)[::-1][:TOP_SAMPLES]
        scores.append((person, float(np.mean(similarities))))
    scores.sort(key=lambda item: item[1], reverse=True)
    return scores


def decide(
    embedding: np.ndarray,
    speech_s: float,
    profiles: Mapping[str, np.ndarray],
    thresholds: Thresholds,
) -> Decision:
    scores = person_scores(embedding, profiles)
    if not scores:
        return Decision(NO_PROFILES)

    best, score = scores[0]
    second = second_score = margin = None
    if len(scores) > 1:
        second, second_score = scores[1]
        margin = score - second_score

    if score < thresholds.t_maybe:
        status = UNKNOWN
    elif (
        score >= thresholds.t_known
        and (margin is None or margin >= thresholds.m_gap)
        and speech_s >= thresholds.sure_speech_s
    ):
        status = KNOWN
    else:
        status = NOT_SURE
    return Decision(status, best, score, second, second_score, margin)


def result_block(
    status: str,
    model: str,
    ms: int,
    decision: Decision | None = None,
    speech_s: float | None = None,
    sample_id: str | None = None,
) -> dict[str, Any]:
    """The nova_voice_id block added to a transcript."""
    block: dict[str, Any] = {"v": RESULT_VERSION, "status": status}
    if decision is not None and decision.score is not None:
        if status == KNOWN:
            block["person"] = decision.best
        block["score"] = round(decision.score, 3)
        if decision.second is not None:
            block["second"] = decision.second
            block["second_score"] = round(decision.second_score, 3)
            block["margin"] = round(decision.margin, 3)
    if speech_s is not None:
        block["speech_s"] = round(speech_s, 2)
    if sample_id is not None:
        block["sample_id"] = sample_id
    block["model"] = model
    block["ms"] = ms
    return block
