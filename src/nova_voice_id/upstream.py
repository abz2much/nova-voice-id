"""Talking to the upstream Wyoming speech to text server (the Whisper app)."""

import asyncio
import dataclasses
import logging
import time
from collections.abc import Callable

from wyoming.client import AsyncClient
from wyoming.info import AsrProgram, Attribution, Describe, Info

from . import __version__

_LOGGER = logging.getLogger(__name__)

PROGRAM_NAME = "Nova Voice ID"
PROGRAM_DESCRIPTION = "Speech to text proxy for Nova"
ATTRIBUTION = Attribution(name="abz2much", url="https://github.com/abz2much/nova-voice-id")

CONNECT_TIMEOUT = 5.0
DESCRIBE_TIMEOUT = 3.0
DESCRIBE_CACHE_SECONDS = 60.0


def make_client(uri: str) -> AsyncClient:
    """Make a client for one upstream connection. Call connect() to open it."""
    return AsyncClient.from_uri(uri, connect_timeout=CONNECT_TIMEOUT)


async def fetch_info(uri: str) -> Info:
    """Ask the upstream for its Info. Raises OSError or TimeoutError on failure."""
    client = make_client(uri)
    try:
        async with asyncio.timeout(DESCRIBE_TIMEOUT):
            await client.connect()
            await client.write_event(Describe().event())
            while True:
                event = await client.read_event()
                if event is None:
                    raise ConnectionError("upstream closed before sending info")
                if Info.is_type(event.type):
                    return Info.from_event(event)
    finally:
        await client.disconnect()


def proxy_info(upstream: Info) -> Info:
    """Our Info: the upstream's speech to text models under our own name."""
    if not upstream.asr:
        return empty_info()

    models = [model for program in upstream.asr for model in program.models]
    program = dataclasses.replace(
        upstream.asr[0],
        name=PROGRAM_NAME,
        description=PROGRAM_DESCRIPTION,
        attribution=ATTRIBUTION,
        installed=True,
        version=__version__,
        models=models,
    )
    return Info(asr=[program])


def empty_info() -> Info:
    """Our Info when the upstream can't be reached: no models."""
    return Info(
        asr=[
            AsrProgram(
                name=PROGRAM_NAME,
                description=PROGRAM_DESCRIPTION,
                attribution=ATTRIBUTION,
                installed=True,
                version=__version__,
                models=[],
            )
        ]
    )


class DescribeCache:
    """Keeps our Info for a short time, so describes don't all hit the upstream.

    Only a good answer is kept. When the upstream is down, every describe tries
    again, so a fresh start of the Whisper app is seen at once.
    """

    def __init__(
        self,
        uri: str,
        max_age: float = DESCRIBE_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._uri = uri
        self._max_age = max_age
        self._clock = clock
        self._info: Info | None = None
        self._fetched_at = 0.0

    async def get(self) -> Info:
        now = self._clock()
        if self._info is not None and now - self._fetched_at < self._max_age:
            return self._info

        try:
            upstream = await fetch_info(self._uri)
        except (OSError, TimeoutError, KeyError, TypeError, ValueError) as err:
            _LOGGER.warning("Upstream describe failed: %s", err.__class__.__name__)
            return empty_info()

        self._info = proxy_info(upstream)
        self._fetched_at = now
        return self._info
