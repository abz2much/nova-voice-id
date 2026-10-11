"""Health check for the image: does the proxy answer a describe?

Exit 0 if it answers with our program name, 1 if not.
"""

import asyncio
import sys

from wyoming.client import AsyncClient
from wyoming.info import Describe, Info

from .upstream import PROGRAM_NAME

URI = "tcp://127.0.0.1:10350"
TIMEOUT = 8.0


async def answers_describe(uri: str = URI) -> bool:
    client = AsyncClient.from_uri(uri)
    try:
        async with asyncio.timeout(TIMEOUT):
            await client.connect()
            await client.write_event(Describe().event())
            while (event := await client.read_event()) is not None:
                if Info.is_type(event.type):
                    info = Info.from_event(event)
                    return any(program.name == PROGRAM_NAME for program in info.asr)
            return False
    except (OSError, TimeoutError):
        return False
    finally:
        await client.disconnect()


def main() -> None:
    sys.exit(0 if asyncio.run(answers_describe()) else 1)


if __name__ == "__main__":
    main()
