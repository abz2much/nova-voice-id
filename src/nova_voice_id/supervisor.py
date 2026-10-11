"""Talking to the Home Assistant Supervisor: discovery, and showing the token.

Both use the app's own Supervisor token. The management token is never
logged, and only travels in request bodies, never in URLs or arguments.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_URL = "http://supervisor"
DISCOVERY_SERVICE = "nova"
TIMEOUT_S = 10.0


class SupervisorError(Exception):
    pass


def available() -> bool:
    return bool(os.environ.get("SUPERVISOR_TOKEN"))


def _call(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(
        SUPERVISOR_URL + path,
        method=method,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.environ['SUPERVISOR_TOKEN']}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            reply = json.loads(response.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError) as err:
        raise SupervisorError(err.__class__.__name__) from None
    if reply.get("result") != "ok":
        raise SupervisorError("Supervisor said no")
    return reply.get("data") or {}


def send_discovery(uri: str, token: str) -> None:
    """Tell Home Assistant (Nova) where the app is and what the token is."""
    _call(
        "POST", "/discovery", {"service": DISCOVERY_SERVICE, "config": {"uri": uri, "token": token}}
    )


def show_token(token: str) -> bool:
    """Copy the token into the app's masked `token` option.

    Returns True if the option was changed. The token file stays the real
    copy; this is only so the user can read it in the Configuration tab.
    """
    options = dict(_call("GET", "/addons/self/info").get("options") or {})
    if options.get("token") == token:
        return False
    options["token"] = token
    _call("POST", "/addons/self/options", {"options": options})
    return True
