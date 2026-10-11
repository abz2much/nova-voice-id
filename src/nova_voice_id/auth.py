"""The management token: 32 random bytes, kept in /data/token (mode 0600)."""

import hmac
import os
import re
import secrets
from pathlib import Path

TOKEN_BYTES = 32
TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")


def load_or_create(path: Path) -> str:
    """Read the token, or make one on first start. Never logged."""
    if path.exists():
        token = path.read_text(encoding="ascii").strip()
        if TOKEN_RE.fullmatch(token):
            os.chmod(path, 0o600)
            return token
        path.unlink()  # damaged: make a new one

    token = secrets.token_hex(TOKEN_BYTES)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(token + "\n")
    return token


def matches(given: object, token: str) -> bool:
    """Constant-time check of a token from a client."""
    if not isinstance(given, str):
        return False
    return hmac.compare_digest(given.encode("utf-8"), token.encode("ascii"))
