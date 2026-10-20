"""Turn python-garminconnect's exceptions into this app's error categories.

Reads and writes are classified differently. A failed read is just an
error. A failed write has to say whether Garmin might have applied it:
`WriteRejected` only when it certainly didn't (Garmin said no with a 4xx, or
no connection was ever made), `WriteOutcomeUnknown` for everything else (a
timeout after the request went out, a dropped connection, a 5xx).
"""

import requests
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)
from urllib3.exceptions import NameResolutionError, NewConnectionError

from garmin_mcp.errors import (
    AuthenticationRequired,
    GarminApiError,
    GarminMcpError,
    InvalidInput,
    NetworkError,
    NotFound,
    NotFoundKind,
    RateLimited,
    WriteOutcomeUnknown,
    WriteRejected,
)

REAUTH = (
    "Run `uv run --package garmin-mcp garmin-mcp-login` in a terminal, "
    "then restart the MCP server."
)


def network_cause(exc: BaseException) -> requests.RequestException | None:
    """The raw network error behind `exc`, if any. The library often wraps
    one in its own (even in an authentication error, when the profile can't
    be fetched), and an outage shouldn't send anyone to sign in again."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, requests.ConnectionError | requests.Timeout):
            return cur
        cur = cur.__cause__ or cur.__context__
    return None


def status_code(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


def _never_connected(exc: requests.RequestException) -> bool:
    """True when the request can't have reached Garmin: the TCP connection
    (or the DNS lookup before it) failed."""
    if isinstance(exc, requests.ConnectTimeout):
        return True
    if not isinstance(exc, requests.ConnectionError):
        return False
    reason = getattr(exc.args[0], "reason", None) if exc.args else None
    return isinstance(reason, NewConnectionError | NameResolutionError)


def classify_read(exc: Exception, kind: NotFoundKind | None) -> GarminMcpError:
    if isinstance(exc, GarminMcpError):
        return exc
    if isinstance(exc, GarminConnectTooManyRequestsError):
        return RateLimited()
    if network_cause(exc) is not None:
        return NetworkError(f"Couldn't reach Garmin Connect ({type(exc).__name__}).")
    if isinstance(exc, GarminConnectAuthenticationError):
        return AuthenticationRequired(f"Garmin rejected the session. {REAUTH}")
    if isinstance(exc, GarminConnectNotFoundError) and kind is not None:
        return NotFound(kind, "Garmin has no such item.")
    if isinstance(exc, GarminConnectConnectionError):
        code = status_code(exc)
        if code == 401:
            return AuthenticationRequired(f"Garmin rejected the session. {REAUTH}")
        if code == 429:
            return RateLimited()
        return GarminApiError(str(exc))
    if isinstance(exc, requests.JSONDecodeError):
        # A ValueError too, but Garmin's answer was unreadable, not the input.
        return GarminApiError("Garmin sent a response that isn't valid JSON.")
    if isinstance(exc, ValueError):
        # The library validates ids and dates before sending anything.
        return InvalidInput(str(exc))
    raise exc


def classify_write(exc: Exception) -> WriteRejected | WriteOutcomeUnknown:
    if isinstance(exc, WriteRejected | WriteOutcomeUnknown):
        return exc
    if isinstance(exc, GarminConnectTooManyRequestsError):
        return WriteRejected("Garmin rate limited the request; nothing changed.")
    if isinstance(exc, GarminConnectAuthenticationError):
        return WriteRejected(f"Garmin rejected the session. {REAUTH}")
    if isinstance(exc, requests.JSONDecodeError):
        # Raised after Garmin answered 2xx, while parsing the body: the
        # request went through, so this is not "rejected before sending".
        return WriteOutcomeUnknown(
            "Garmin accepted the request but its answer wasn't valid JSON; "
            "it may or may not have applied the change."
        )
    if isinstance(exc, ValueError):
        return WriteRejected(f"Rejected before sending: {exc}")
    cause = network_cause(exc)
    if cause is not None:
        if _never_connected(cause):
            return WriteRejected(
                "Couldn't connect to Garmin Connect; nothing was sent."
            )
        return WriteOutcomeUnknown(
            f"The connection failed mid-request ({type(cause).__name__}); "
            f"Garmin may or may not have applied it."
        )
    if isinstance(exc, GarminConnectConnectionError):
        code = status_code(exc)
        if code is not None and code < 500:
            return WriteRejected(f"Garmin refused the change: {exc}")
        return WriteOutcomeUnknown(
            f"Garmin answered with an error ({exc}); it may or may not have "
            f"applied the change."
        )
    return WriteOutcomeUnknown(
        f"Unexpected failure ({type(exc).__name__}); the change may or may "
        f"not have been applied."
    )
