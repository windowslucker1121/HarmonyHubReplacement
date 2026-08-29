"""Proves an update request came from someone holding the device's token.

The token itself never travels -- not in the request, not in any API
response. Only an HMAC over data both sides already have (a nonce, and the
bundle's own content hash) crosses the wire, and only a short fingerprint of
the token is ever shown, in the Settings screen, so a human can confirm the
two ends were set up with the same secret without either end reading it back.

Deliberately clock-free. The device this was designed for (a Pi 3A+ with no
RTC) may not have correct time until NTP syncs after boot, so replay
protection is a strictly increasing nonce persisted alongside the release
state, not a timestamp window.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger("HUB.update.auth")

TOKEN_BYTES = 32


class InvalidSignature(ValueError):
    """The request's signature does not match, or its nonce has been used before."""


def generate_token() -> bytes:
    return secrets.token_bytes(TOKEN_BYTES)


def load_or_create_token(path: "Path | str") -> bytes:
    """Reads the device's update token, generating one on first use.

    Mode 600 on creation: this file is the entire access control for
    `/api/update`, and it lives next to `hub_settings.json` rather than in
    the code tree specifically so an update can never overwrite it.
    """
    path = Path(path)
    if path.exists():
        token = path.read_bytes()
        if len(token) != TOKEN_BYTES:
            raise ValueError(f"{path} does not look like an update token ({len(token)} bytes)")
        return token

    token = generate_token()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written directly rather than via storage.write_json: this is raw
    # bytes, and the temp-file-then-rename dance there is JSON-specific.
    # A torn write here just means "no token yet", not a corrupt state file.
    #
    # `O_BINARY` matters on Windows: `os.open` defaults to text mode there,
    # which rewrites a lone `\n` byte to `\r\n` -- silently corrupting
    # whichever token happens to contain one. POSIX has no such mode and no
    # such flag, hence the `getattr` fallback to 0.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(str(path), flags, 0o600)
    try:
        os.write(fd, token)
    finally:
        os.close(fd)
    logger.info("Generated a new update token at %s", path)
    return token


def fingerprint(token: bytes) -> str:
    """A short, non-secret identifier for a token -- enough to confirm two ends match, not to derive it."""
    return hashlib.sha256(token).hexdigest()[:8]


def sign(token: bytes, nonce: int, content_sha256: str) -> str:
    message = f"{nonce}:{content_sha256}".encode("ascii")
    return hmac.new(token, message, hashlib.sha256).hexdigest()


def verify(token: bytes, nonce: int, content_sha256: str, signature: str, last_nonce: int) -> None:
    """Raises `InvalidSignature` rather than returning a bool, so a caller cannot forget to check it."""
    if nonce <= last_nonce:
        raise InvalidSignature(f"nonce {nonce} has already been used (last accepted: {last_nonce})")
    expected = sign(token, nonce, content_sha256)
    if not hmac.compare_digest(expected, signature):
        raise InvalidSignature("signature does not match")
