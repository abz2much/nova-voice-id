"""One handler per client connection.

A transcribe request gets its own upstream connection. Client events go up
the moment they arrive, and upstream events come back from a reader task that
runs at the same time. On audio-stop the speaker step starts while the
upstream decodes; the transcript waits at most 400 ms more for its result.
Nothing about one request is shared with another.

Never log the words, the audio, the token, or names with scores.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient
from wyoming.error import Error
from wyoming.event import Event
from wyoming.info import Describe
from wyoming.server import AsyncEventHandler

from .management import Management, is_management
from .speaker import RequestAudio
from .upstream import DescribeCache, make_client
from .voiceid import Outcome, VoiceId

_LOGGER = logging.getLogger(__name__)

RESULT_KEY = "nova_voice_id"
UPSTREAM_UNAVAILABLE = "upstream_unavailable"


class ProxyHandler(AsyncEventHandler):
    """Proxies one client connection to the upstream."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        upstream_uri: str,
        describe_cache: DescribeCache,
        voiceid: VoiceId,
        management: Management,
    ) -> None:
        super().__init__(reader, writer)
        self._upstream_uri = upstream_uri
        self._describe_cache = describe_cache
        self._voiceid = voiceid
        self._management = management
        self._request: _Request | None = None

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            info = await self._describe_cache.get()
            await self.write_event(info.event())
            return True

        if is_management(event):
            await self.write_event(await self._management.handle(event))
            return True

        # A transcribe starts a request. So does audio-start on its own, for
        # clients that skip transcribe.
        starts_request = Transcribe.is_type(event.type) or (
            AudioStart.is_type(event.type) and (self._request is None or self._request.finished)
        )
        if starts_request:
            await self._end_request("replaced")
            self._request = _Request(self._upstream_uri, self._voiceid, self.write_event, self.stop)
            if not await self._request.open():
                return False

        if self._request is None:
            _LOGGER.debug("Ignored %s outside a request", event.type)
            return True

        return await self._request.send(event)

    async def disconnect(self) -> None:
        await self._end_request("client_closed")

    async def _end_request(self, status: str) -> None:
        request, self._request = self._request, None
        if request is not None:
            await request.close(status)


class _Request:
    """One transcribe request and its upstream connection."""

    def __init__(
        self,
        upstream_uri: str,
        voiceid: VoiceId,
        write_to_client: Callable[[Event], Awaitable[None]],
        stop_client: Callable[[], Awaitable[None]],
    ) -> None:
        self._upstream_uri = upstream_uri
        self._voiceid = voiceid
        self._write_to_client = write_to_client
        self._stop_client = stop_client
        self._client: AsyncClient | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._started = time.monotonic()
        self._finished = False
        self._speaker_task: asyncio.Task[Outcome] | None = None
        self._speaker_started = 0.0
        self._speaker_status = "none"
        self.audio = RequestAudio()

    @property
    def finished(self) -> bool:
        return self._finished

    async def open(self) -> bool:
        """Connect to the upstream. On failure, tell the client and finish."""
        client = make_client(self._upstream_uri)
        try:
            await client.connect()
        except (OSError, TimeoutError) as err:
            _LOGGER.warning("Can't reach upstream: %s", err.__class__.__name__)
            await self._fail_upstream()
            return False

        self._client = client
        self._reader_task = asyncio.create_task(self._read_upstream())
        return True

    async def send(self, event: Event) -> bool:
        """Send a client event up, unchanged."""
        if self._finished or self._client is None:
            return True

        try:
            await self._client.write_event(event)
        except OSError as err:
            if self._finished:
                # The transcript is already back and the upstream closed.
                return True
            _LOGGER.warning("Upstream write failed: %s", err.__class__.__name__)
            await self._fail_upstream()
            return False

        if AudioChunk.is_type(event.type):
            self.audio.add(event)
        elif AudioStop.is_type(event.type):
            # Start the speaker step while the upstream decodes.
            self._start_speaker()

        return True

    def _start_speaker(self) -> None:
        if self._speaker_task is not None:
            return
        self._speaker_started = time.monotonic()
        if self.audio.usable:
            self._speaker_task = asyncio.create_task(self._voiceid.analyse(self.audio.pcm()))

    async def close(self, status: str) -> None:
        """End the request on any path. Safe to call more than once."""
        task = self._reader_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._finish(status)

    async def _read_upstream(self) -> None:
        """Send upstream events back to the client until the request ends."""
        assert self._client is not None
        try:
            while True:
                event = await self._client.read_event()
                if event is None:
                    _LOGGER.warning("Upstream closed before sending a transcript")
                    await self._fail_upstream()
                    return

                if Transcript.is_type(event.type):
                    self._start_speaker()  # if the transcript came before audio-stop
                    block = await self._voiceid.result_for(
                        self._speaker_task, self._speaker_started
                    )
                    self._speaker_status = block["status"]
                    data = dict(event.data)
                    data[RESULT_KEY] = block
                    await self._write_to_client(Event(type="transcript", data=data))
                    await self._finish("ok")
                    return

                if Error.is_type(event.type):
                    _LOGGER.warning("Upstream error, code %s", event.data.get("code"))
                    await self._write_to_client(event)
                    await self._finish("upstream_error")
                    return

                # Anything else (transcript-start, -chunk, -stop) goes back unchanged.
                await self._write_to_client(event)
        except OSError as err:
            _LOGGER.warning("Upstream read failed: %s", err.__class__.__name__)
            await self._fail_upstream()

    async def _fail_upstream(self) -> None:
        """Tell the client the upstream is unavailable, then close it."""
        if self._finished:
            return
        await self._finish(UPSTREAM_UNAVAILABLE)
        try:
            await self._write_to_client(
                Error(text="Upstream unavailable", code=UPSTREAM_UNAVAILABLE).event()
            )
        except OSError:
            pass
        await self._stop_client()

    async def _finish(self, status: str) -> None:
        """Close the upstream, drop the audio, and log one line."""
        if self._finished:
            return
        self._finished = True

        task, self._speaker_task = self._speaker_task, None
        if task is not None and not task.done():
            task.cancel()  # the worker thread finishes on its own; its result is dropped

        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except OSError:
                pass

        seconds = self.audio.seconds
        self.audio.clear()
        elapsed_ms = round((time.monotonic() - self._started) * 1000)
        _LOGGER.info(
            "Request: %d ms, %.2f s audio, status %s, speaker %s",
            elapsed_ms,
            seconds,
            status,
            self._speaker_status,
        )
