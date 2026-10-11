"""Test helpers: a fake upstream Wyoming server, the proxy on a free port, a
Wyoming client, fake and real speaker engines, and the voice fixtures."""

import asyncio
import hashlib
import os
import time
import wave
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pytest
from wyoming.asr import Transcribe
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event, async_read_event, async_write_event
from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info

from nova_voice_id.handler import ProxyHandler
from nova_voice_id.management import Management
from nova_voice_id.speaker import MODELS, Analysis, Engine, Thresholds, pcm_to_float, unit
from nova_voice_id.upstream import DescribeCache
from nova_voice_id.voiceid import VoiceId

RATE, WIDTH, CHANNELS = 16000, 2, 1
READ_TIMEOUT = 10
CHUNK_BYTES = 1024
TOKEN = "ab" * 32
FIXTURES = Path(__file__).parent / "fixtures"
MODELS_DIR = Path(os.environ.get("NVID_MODELS_DIR", Path(__file__).parent.parent / ".models"))

# Thresholds for the fixture voices with the default model. Voices A and B
# score 0.66 to 0.78 against themselves; unenrolled voice C reaches 0.58.
TEST_THRESHOLDS = Thresholds(
    t_known=0.62, t_maybe=0.45, m_gap=0.10, min_speech_s=0.8, sure_speech_s=1.2, trim_start_ms=300
)

UPSTREAM_INFO = Info(
    asr=[
        AsrProgram(
            name="faster-whisper",
            description="Faster Whisper",
            attribution=Attribution(name="test", url="https://example.invalid"),
            installed=True,
            version="3.5.0",
            models=[
                AsrModel(
                    name="base-int8",
                    description="base",
                    attribution=Attribution(name="test", url="https://example.invalid"),
                    installed=True,
                    version="1",
                    languages=["en", "fr", "de"],
                )
            ],
            requires_external_vad=False,
        )
    ]
)


def load_pcm(name: str) -> bytes:
    with wave.open(str(FIXTURES / f"{name}.wav"), "rb") as wav:
        assert (wav.getframerate(), wav.getsampwidth(), wav.getnchannels()) == (16000, 2, 1)
        return wav.readframes(wav.getnframes())


def words_for(audio: bytes) -> str:
    """The fake upstream's transcript for some audio."""
    return "words-" + hashlib.sha256(audio).hexdigest()[:12]


@dataclass
class UpstreamConnection:
    """What the fake upstream saw on one connection."""

    events: list[Event] = field(default_factory=list)
    first_chunk: asyncio.Event = field(default_factory=asyncio.Event)
    stop_seen_before_first_chunk: bool = False
    transcript_sent_at: float | None = None


class FakeUpstream:
    """Fake speech to text server.

    The transcript is "words-" plus a hash of all the audio, so each answer
    can be traced back to its own audio.
    """

    def __init__(self) -> None:
        self.connections: list[UpstreamConnection] = []
        self.connected = asyncio.Event()
        self.describes = 0
        # How many transcribe connections must be open before any is answered.
        self.wait_for_connections = 1
        self.reply_error = False
        self.close_after_audio_start = False
        self.decode_delay = 0.0
        self._open = 0
        self._enough_open = asyncio.Event()
        self._server: asyncio.Server | None = None

    @property
    def uri(self) -> str:
        assert self._server is not None
        port = self._server.sockets[0].getsockname()[1]
        return f"tcp://127.0.0.1:{port}"

    def answer_after(self, count: int) -> None:
        """Answer no one until `count` more transcribe requests are open."""
        self.wait_for_connections = self._open + count
        self._enough_open.clear()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            # Drop open connections too, or a failed test hangs here.
            self._server.close_clients()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = UpstreamConnection()
        self.connections.append(conn)
        self.connected.set()
        audio = bytearray()
        language = None
        try:
            while True:
                event = await async_read_event(reader)
                if event is None:
                    return
                conn.events.append(event)

                if Describe.is_type(event.type):
                    self.describes += 1
                    await async_write_event(UPSTREAM_INFO.event(), writer)
                elif Transcribe.is_type(event.type):
                    language = Transcribe.from_event(event).language
                    self._open += 1
                    if self._open >= self.wait_for_connections:
                        self._enough_open.set()
                elif event.type == "audio-start" and self.close_after_audio_start:
                    return
                elif AudioChunk.is_type(event.type):
                    audio.extend(event.payload or b"")
                    conn.first_chunk.set()
                elif AudioStop.is_type(event.type):
                    if not conn.first_chunk.is_set():
                        conn.stop_seen_before_first_chunk = True
                    await asyncio.wait_for(self._enough_open.wait(), timeout=5)
                    await asyncio.sleep(self.decode_delay)
                    if self.reply_error:
                        reply = Event(type="error", data={"text": "boom", "code": "test_error"})
                    else:
                        reply = Event(
                            type="transcript",
                            data={"text": words_for(bytes(audio)), "language": language},
                        )
                    conn.transcript_sent_at = time.monotonic()
                    await async_write_event(reply, writer)
        finally:
            writer.close()


class FakeEngine(Engine):
    """The real pool, with a stand-in for the models.

    The embedding comes from the audio itself, so equal audio gives equal
    embeddings. `delay` makes analysis slow; `fail` makes it raise.
    """

    def __init__(self, model: str = "fake_model") -> None:
        super().__init__(Path("/nonexistent"), model)
        self.delay = 0.0
        self.fail = False

    def warm_up(self) -> None:
        pass

    def analyse(self, pcm: bytes, thresholds: Thresholds) -> Analysis:
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("model crashed")
        trimmed = pcm[int(thresholds.trim_start_ms * 16) * 2 :]
        speech_s = len(trimmed) / (RATE * WIDTH)
        if speech_s < thresholds.min_speech_s:
            return Analysis(trimmed, speech_s, None)
        return Analysis(trimmed, speech_s, self.embed_recording(trimmed))

    def embed_recording(self, pcm: bytes) -> np.ndarray | None:
        samples = pcm_to_float(pcm)
        if len(samples) == 0:
            return None
        seed = int.from_bytes(hashlib.sha256(pcm).digest()[:4], "little")
        return unit(np.random.default_rng(seed).standard_normal(8).astype(np.float32))


class ProxyServer:
    """The proxy on a free local port."""

    def __init__(self, upstream_uri: str, voiceid: VoiceId, token: str = TOKEN) -> None:
        self.voiceid = voiceid
        self.describe_cache = DescribeCache(upstream_uri)
        self.management = Management(voiceid, token)
        self._upstream_uri = upstream_uri
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handler = ProxyHandler(
            reader,
            writer,
            upstream_uri=self._upstream_uri,
            describe_cache=self.describe_cache,
            voiceid=self.voiceid,
            management=self.management,
        )
        await handler.run()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            # Drop open connections too, or a failed test hangs here.
            self._server.close_clients()
            await self._server.wait_closed()


class Client:
    """A Wyoming client talking to the proxy."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, port: int) -> "Client":
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        return cls(reader, writer)

    async def send(self, event: Event) -> None:
        await async_write_event(event, self.writer)

    async def read(self) -> Event | None:
        return await asyncio.wait_for(async_read_event(self.reader), timeout=READ_TIMEOUT)

    async def close(self) -> None:
        self.writer.close()


def chunk(audio: bytes) -> Event:
    return AudioChunk(rate=RATE, width=WIDTH, channels=CHANNELS, audio=audio).event()


def split(pcm: bytes) -> list[bytes]:
    return [pcm[i : i + CHUNK_BYTES] for i in range(0, len(pcm), CHUNK_BYTES)]


async def transcribe(port: int, audio: list[bytes] | bytes, language: str = "en") -> Event:
    """One full request. Returns the reply event."""
    if isinstance(audio, bytes):
        audio = split(audio)
    client = await Client.connect(port)
    await client.send(Transcribe(language=language).event())
    await client.send(AudioStart(rate=RATE, width=WIDTH, channels=CHANNELS).event())
    for piece in audio:
        await client.send(chunk(piece))
    await client.send(AudioStop().event())
    reply = await client.read()
    await client.close()
    assert reply is not None
    return reply


async def manage(port: int, event_type: str, **data) -> Event:
    """Send one management event and return the reply."""
    client = await Client.connect(port)
    await client.send(Event(type=event_type, data=data))
    reply = await client.read()
    await client.close()
    assert reply is not None
    return reply


def tree(path: Path) -> dict[str, bytes]:
    """Every file under a folder with its bytes, to check nothing changed."""
    return {
        str(p.relative_to(path)): p.read_bytes()
        for p in sorted(path.rglob("*"))
        if p.is_file() and not p.name.startswith("scores.db")
    }


def real_models_available() -> bool:
    return all((MODELS_DIR / f"{m}.onnx").exists() for m in (*MODELS, "silero_vad"))


@pytest.fixture
def need_models() -> None:
    if not real_models_available():
        if os.environ.get("NVID_REQUIRE_MODELS") == "1":
            pytest.fail(f"Models missing from {MODELS_DIR}")
        pytest.skip("Models not downloaded (run scripts/fetch_models.py)")


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    server = FakeUpstream()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
def fake_engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def make_voiceid(data_dir: Path) -> Callable[..., VoiceId]:
    made: list[VoiceId] = []

    def make(engine: Engine, thresholds: Thresholds = TEST_THRESHOLDS, **kwargs) -> VoiceId:
        voiceid = VoiceId(data_dir, engine, thresholds, **kwargs)
        made.append(voiceid)
        return voiceid

    yield make
    for voiceid in made:
        voiceid.close()


@pytest.fixture
async def proxy(upstream: FakeUpstream, fake_engine: FakeEngine, make_voiceid):
    voiceid = make_voiceid(fake_engine)
    await voiceid.start()
    server = ProxyServer(upstream.uri, voiceid)
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
async def real_proxy(upstream: FakeUpstream, need_models, make_voiceid):
    # These tests check recognition, not machine speed, so the speaker step
    # gets a long window. The 400 ms rule has its own test with the fake engine.
    voiceid = make_voiceid(Engine(MODELS_DIR, MODELS[0]), result_wait_s=5.0)
    await voiceid.start()
    server = ProxyServer(upstream.uri, voiceid)
    await server.start()
    yield server
    await server.stop()
