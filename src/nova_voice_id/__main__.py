"""Start the Nova Voice ID proxy."""

import argparse
import asyncio
import contextlib
import logging
import socket
from functools import partial
from urllib.parse import urlparse

from wyoming.server import AsyncServer

from . import __version__
from .handler import ProxyHandler
from .upstream import DescribeCache, fetch_info

# Start-up lines show at every log level, so the user can always see the
# address to give Nova and whether the upstream answered.
_START_LOGGER = logging.getLogger("nova_voice_id.start")
_START_LOGGER.setLevel(logging.INFO)

UPSTREAM_RETRY_SECONDS = 5.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="nova_voice_id")
    parser.add_argument("--uri", default="tcp://0.0.0.0:10350", help="Address to listen on")
    parser.add_argument(
        "--upstream-uri", default="tcp://core-whisper:10300", help="Wyoming STT server"
    )
    parser.add_argument(
        "--log-level", default="warning", choices=["debug", "info", "warning", "error"]
    )
    return parser.parse_args()


async def check_upstream(uri: str) -> None:
    """Log once when the upstream first answers. Warn once if it doesn't yet."""
    warned = False
    while True:
        try:
            info = await fetch_info(uri)
        except (OSError, TimeoutError, KeyError, TypeError, ValueError) as err:
            if not warned:
                _START_LOGGER.warning(
                    "Can't reach upstream at %s yet (%s). Will keep trying.",
                    uri,
                    err.__class__.__name__,
                )
                warned = True
            await asyncio.sleep(UPSTREAM_RETRY_SECONDS)
            continue

        models = sum(len(program.models) for program in info.asr)
        _START_LOGGER.info("Reached upstream at %s (%d speech to text models)", uri, models)
        return


async def run(args: argparse.Namespace) -> None:
    port = urlparse(args.uri).port
    _START_LOGGER.info(
        "Nova Voice ID %s listening on tcp://%s:%s", __version__, socket.gethostname(), port
    )

    describe_cache = DescribeCache(args.upstream_uri)
    server = AsyncServer.from_uri(args.uri)
    checker = asyncio.create_task(check_upstream(args.upstream_uri))
    try:
        await server.run(
            partial(ProxyHandler, upstream_uri=args.upstream_uri, describe_cache=describe_cache)
        )
    finally:
        checker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await checker


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
