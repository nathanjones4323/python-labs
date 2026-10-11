"""The failures a caller can do something about.

Tools turn these into structured MCP errors (`tools.as_tool_error`), keeping
the class name as the error code so an agent can branch on it instead of
reading prose.
"""


class DatePlannerError(Exception):
    """Base class for everything a tool reports back to its caller."""


class NotFound(DatePlannerError):
    """A lookup by id matched nothing."""


class InvalidTransition(DatePlannerError):
    """A date was asked to move somewhere its state machine forbids."""


class DetectionFailed(DatePlannerError):
    """A restaurant's booking channel couldn't be worked out — no website, or
    nothing fetchable."""


class UnsupportedChannel(DatePlannerError):
    """There is no deep link to build for this channel (phone, walk-in,
    unknown)."""


class MissingPlatformSlug(DatePlannerError):
    """The channel supports deep links but the restaurant has no slug for it."""
