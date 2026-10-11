"""A fake upstream Wyoming server and helpers to run the proxy in tests."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from wyoming.asr import Transcribe
from wyoming.audio import AudioChunk, AudioStop
from wyoming.event import Event, async_read_event, async_write_event
from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info

from nova_voice_id.handler import ProxyHandler
from nova_voice_id.upstream import DescribeCache

RATE, WIDTH, CHANNELS = 16000, 2, 1
READ_TIMEOUT = 5

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


@dataclass
class UpstreamConnection:
    """What the fake upstream saw on one connection."""

    events: list[Event] = field(default_factory=list)
    first_chunk: asyncio.Event = field(default_factory=asyncio.Event)
    stop_seen_before_first_chunk: bool = False


class FakeUpstream:
    """Fake speech to text server.

    The transcript text is "words-" plus the hex of the first audio bytes, so
    each request's answer can be traced back to its own audio.
    """

    def __init__(self) -> None:
        self.connections: list[UpstreamConnection] = []
        self.connected = asyncio.Event()
        self.describes = 0
        # How many transcribe connections must be open before any is answered.
        self.wait_for_connections = 1
        self.reply_error = False
        self.close_after_audio_start = False
        self._open = 0
        self._enough_open = asyncio.Event()
        self._server: asyncio.Server | None = None

    @property
    def uri(self) -> str:
        assert self._server is not None
        port = self._server.sockets[0].getsockname()[1]
        return f"tcp://127.0.0.1:{port}"

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
                    if self.reply_error:
                        reply = Event(type="error", data={"text": "boom", "code": "test_error"})
                    else:
                        reply = Event(
                            type="transcript",
                            data={"text": f"words-{bytes(audio[:4]).hex()}", "language": language},
                        )
                    await async_write_event(reply, writer)
        finally:
            writer.close()


class ProxyServer:
    """The proxy on a free local port."""

    def __init__(self, upstream_uri: str) -> None:
        self.describe_cache = DescribeCache(upstream_uri)
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
            reader, writer, upstream_uri=self._upstream_uri, describe_cache=self.describe_cache
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


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    server = FakeUpstream()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
async def proxy(upstream: FakeUpstream) -> AsyncIterator[ProxyServer]:
    server = ProxyServer(upstream.uri)
    await server.start()
    yield server
    await server.stop()
