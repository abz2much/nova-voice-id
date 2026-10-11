"""Start the Nova Voice ID proxy."""

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import socket
from pathlib import Path
from urllib.parse import urlparse

from wyoming.server import AsyncServer

from . import __version__, auth, supervisor
from .handler import ProxyHandler
from .management import Management
from .speaker import MODELS, Engine, Thresholds
from .upstream import DescribeCache, fetch_info
from .voiceid import VoiceId

# Start-up lines show at every log level, so the user can always see the
# address to give Nova and whether the upstream answered.
_START_LOGGER = logging.getLogger("nova_voice_id.start")
_START_LOGGER.setLevel(logging.INFO)

UPSTREAM_RETRY_SECONDS = 5.0
DISCOVERY_RETRY_SECONDS = 30.0


def parse_args() -> argparse.Namespace:
    defaults = Thresholds()
    parser = argparse.ArgumentParser(prog="nova_voice_id")
    parser.add_argument("--uri", default="tcp://0.0.0.0:10350", help="Address to listen on")
    parser.add_argument(
        "--upstream-uri", default="tcp://core-whisper:10300", help="Wyoming STT server"
    )
    parser.add_argument(
        "--log-level", default="warning", choices=["debug", "info", "warning", "error"]
    )
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    parser.add_argument("--models-dir", type=Path, default=Path("/usr/src/models"))
    parser.add_argument("--model", default=MODELS[0], choices=MODELS)
    parser.add_argument("--t-known", type=float, default=defaults.t_known)
    parser.add_argument("--t-maybe", type=float, default=defaults.t_maybe)
    parser.add_argument("--m-gap", type=float, default=defaults.m_gap)
    parser.add_argument("--min-speech-s", type=float, default=defaults.min_speech_s)
    parser.add_argument("--sure-speech-s", type=float, default=defaults.sure_speech_s)
    parser.add_argument("--trim-start-ms", type=int, default=defaults.trim_start_ms)
    parser.add_argument(
        "--no-supervisor", action="store_true", help="Skip discovery and the token option"
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


async def announce(uri: str, token: str) -> None:
    """Show the token in the app options, then send discovery to Nova."""
    try:
        if await asyncio.to_thread(supervisor.show_token, token):
            _START_LOGGER.info("The token is in the app's Configuration tab")
    except supervisor.SupervisorError as err:
        _START_LOGGER.warning("Could not put the token in the app options (%s)", err)

    while True:
        try:
            await asyncio.to_thread(supervisor.send_discovery, uri, token)
        except supervisor.SupervisorError as err:
            _START_LOGGER.warning("Discovery to Home Assistant failed (%s). Will try again.", err)
            await asyncio.sleep(DISCOVERY_RETRY_SECONDS)
            continue
        _START_LOGGER.info("Sent discovery to Home Assistant for Nova")
        return


async def run(args: argparse.Namespace) -> None:
    port = urlparse(args.uri).port
    public_uri = f"tcp://{socket.gethostname()}:{port}"
    _START_LOGGER.info("Nova Voice ID %s listening on %s", __version__, public_uri)

    args.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = auth.load_or_create(args.data_dir / "token")
    thresholds = Thresholds(
        t_known=args.t_known,
        t_maybe=args.t_maybe,
        m_gap=args.m_gap,
        min_speech_s=args.min_speech_s,
        sure_speech_s=args.sure_speech_s,
        trim_start_ms=args.trim_start_ms,
    )
    voiceid = VoiceId(args.data_dir, Engine(args.models_dir, args.model), thresholds)
    await voiceid.start()
    profiles = len(voiceid.profiles.all())
    _START_LOGGER.info("Speaker model %s, %d voice profiles", args.model, profiles)

    management = Management(voiceid, token)
    describe_cache = DescribeCache(args.upstream_uri)
    server = AsyncServer.from_uri(args.uri)

    def handler(reader, writer):
        return ProxyHandler(
            reader,
            writer,
            upstream_uri=args.upstream_uri,
            describe_cache=describe_cache,
            voiceid=voiceid,
            management=management,
        )

    tasks = [asyncio.create_task(check_upstream(args.upstream_uri))]
    try:
        await server.start(handler)  # the port answers from here on
        if not args.no_supervisor and supervisor.available():
            tasks.append(asyncio.create_task(announce(public_uri, token)))
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal.SIGTERM, stop.set)
        await stop.wait()
        await server.stop()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        voiceid.close()


def main() -> None:
    os.umask(0o077)  # every file the app makes is private
    args = parse_args()
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
