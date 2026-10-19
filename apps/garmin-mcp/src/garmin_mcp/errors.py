"""The failures a caller can act on, one class per category.

Every class carries a stable `code`. `server` reports these as MCP tool
errors with the code leading its message (`not_found: ...`, after the SDK's
"Error executing tool <name>: "), so an agent can branch on the category
instead of parsing prose. Anything that isn't a
`GarminMcpError` is a bug: the SDK hides its details from the client and the
log keeps the traceback.
"""

from typing import ClassVar, Literal

NotFoundKind = Literal["workout", "activity", "exercise", "calendar_entry", "device"]


class GarminMcpError(Exception):
    code: ClassVar[str] = "error"

    def __str__(self) -> str:
        return super().__str__() or self.__doc__ or self.code


class AuthenticationRequired(GarminMcpError):
    """There is no usable Garmin session. Run `garmin-mcp-login` in a terminal
    to sign in again, then restart the MCP server."""

    code = "authentication"


class InvalidInput(GarminMcpError):
    """An argument failed validation; nothing was sent to Garmin."""

    code = "validation"


class NotFound(GarminMcpError):
    code = "not_found"

    def __init__(self, kind: NotFoundKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind

    def __str__(self) -> str:
        return f"{self.kind}: {super().__str__()}"


class Ambiguous(GarminMcpError):
    """A reference matched more than one thing. Pass the exact id instead."""

    code = "ambiguous"


class Conflict(GarminMcpError):
    """The write would duplicate something that already exists. Nothing was
    sent; the message says which flag allows it on purpose."""

    code = "conflict"


class RateLimited(GarminMcpError):
    """Garmin is rate limiting this account. Wait a few minutes; don't retry
    in a loop."""

    code = "rate_limit"


class NetworkError(GarminMcpError):
    """Garmin Connect couldn't be reached. For a read, try again later."""

    code = "network"


class GarminApiError(GarminMcpError):
    """Garmin answered with an error for a read."""

    code = "garmin_api"


class Unsupported(GarminMcpError):
    """The workout, metric or device capability isn't something this server
    can handle."""

    code = "unsupported"


class ApprovalInvalid(GarminMcpError):
    """The confirmation token doesn't authorize this exact change: it expired,
    was already used, or the request or the Garmin state changed since the
    preview. Call again without a token for a fresh preview."""

    code = "approval"


# Raised by the repository for writes, and turned into a result (never a
# tool error) by `services.writes`. They separate "Garmin definitely didn't
# apply it" from "it may have been applied".


class WriteRejected(GarminMcpError):
    """The write definitely didn't happen (a 4xx answer, an auth or rate-limit
    refusal, or no connection was ever made)."""

    code = "failed"


class WriteOutcomeUnknown(GarminMcpError):
    """The request may have reached Garmin but no clear answer came back (a
    timeout, a dropped connection, a 5xx). Never retry it blindly."""

    code = "unknown"
