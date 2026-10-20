"""One request to a JSON API, with its failures turned into our own error.

Routing and the forecast both want the same thing: use the caller's client
if there is one (so a run of requests shares it), otherwise open one for
the call; report a transport failure, an error status or a body that isn't
JSON as the service's `DatePlannerError`.
"""

from collections.abc import Callable
from typing import Any

import httpx

from date_planner.config import get_settings
from date_planner.errors import DatePlannerError


async def request_json(
    client: httpx.AsyncClient | None,
    method: str,
    url: str,
    *,
    failure: type[DatePlannerError],
    service: str,
    reason: Callable[[Any], str | None] | None = None,
    **kwargs: Any,
) -> Any:
    """The decoded body of `method url`: a JSON object or array.

    `reason` picks the service's own explanation out of an error body, if it
    gives one.
    """
    try:
        if client is not None:
            response = await client.request(method, url, **kwargs)
        else:
            async with httpx.AsyncClient(timeout=get_settings().fetch_timeout) as owned:
                response = await owned.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise failure(f"{service} request failed: {exc}") from exc
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.is_error:
        detail = reason(data) if reason is not None and data is not None else None
        raise failure(
            f"{service} request failed: {response.status_code}"
            + (f" {detail}" if detail else "")
        )
    if not isinstance(data, dict | list):
        raise failure(f"{service} returned something other than JSON")
    return data
