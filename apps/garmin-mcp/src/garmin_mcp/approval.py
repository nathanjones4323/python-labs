"""Confirmation tokens: a write runs only on the token from its own preview.

A write tool called without a token returns a preview and a token. The
token is an HMAC over the operation and a fingerprint of exactly what will
be sent, including the Garmin state the preview was built from. Calling
again with the token re-plans the write from scratch, and the token only
works if the fingerprint still matches. So changed arguments, or a calendar
that changed in between, need a fresh preview. Each token works once (a
second call with it is refused instead of writing twice) and expires after
`GARMIN_APPROVAL_TTL_SECONDS`.

The tokens and the key live in this process only; a restart voids them.

This is what makes approval explicit at the protocol level. Whether a
person approved is up to the client: the tool descriptions tell the agent
to show the preview and wait, and Claude Code also asks before calling any
tool not on its allow list (see README, "Approval").
"""

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from garmin_mcp.errors import ApprovalInvalid


def fingerprint(operation: str, plan: Any) -> str:
    body = json.dumps([operation, plan], sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()


class ApprovalBook:
    def __init__(
        self, ttl_seconds: int, clock: Callable[[], float] = time.time
    ) -> None:
        self._key = secrets.token_bytes(32)
        self._ttl = ttl_seconds
        self._clock = clock
        self._used: dict[str, float] = {}  # nonce -> expiry
        self._lock = threading.Lock()

    def issue(self, operation: str, plan_fingerprint: str) -> tuple[str, datetime]:
        nonce = secrets.token_urlsafe(12)
        expires = int(self._clock()) + self._ttl
        sig = self._sign(operation, plan_fingerprint, nonce, expires)
        return f"{nonce}.{expires}.{sig}", datetime.fromtimestamp(expires, UTC)

    def consume(self, token: str, operation: str, plan_fingerprint: str) -> None:
        try:
            nonce, expires_s, sig = token.split(".")
            expires = int(expires_s)
            # compare_digest raises TypeError on non-ASCII str.
            sig.encode("ascii")
        except ValueError:  # UnicodeEncodeError included
            raise ApprovalInvalid(
                "That isn't a confirmation token from this server."
            ) from None
        now = self._clock()
        with self._lock:
            self._used = {n: e for n, e in self._used.items() if e > now}
            # Checked first: after a write, the state it was planned against
            # has usually changed too, and "already used" is the useful news.
            if nonce in self._used:
                raise ApprovalInvalid(
                    "This token was already used. The change was attempted "
                    "once; check its result before doing anything else."
                )
            expected = self._sign(operation, plan_fingerprint, nonce, expires)
            if not hmac.compare_digest(sig, expected):
                raise ApprovalInvalid(
                    "The token doesn't match this change: the arguments or the "
                    "Garmin state differ from the preview it came with (or the "
                    "server restarted). Call again without a token for a fresh "
                    "preview and show it to the user."
                )
            if expires <= now:
                raise ApprovalInvalid(
                    "The token expired. Call again without a token for a fresh preview."
                )
            self._used[nonce] = expires

    def _sign(
        self, operation: str, plan_fingerprint: str, nonce: str, expires: int
    ) -> str:
        message = f"{operation}|{plan_fingerprint}|{nonce}|{expires}".encode()
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()[:32]
