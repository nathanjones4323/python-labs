"""The operation log: one JSON line per write attempt, in
`$GARMIN_STATE_DIR/operations.jsonl` (0600).

It records what was attempted and how it ended (operation, status, the ids
and dates involved), so an `unknown` outcome can be looked into later. It
never records workout or set contents, health data, tokens or confirmation tokens.
"""

import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Only these keys of a write's `result` are copied into the log.
_ID_KEYS = (
    "activity_id",
    "workout_id",
    "scheduled_workout_id",
    "device_id",
    "date",
    "from_date",
    "to_date",
)


class OperationLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def record(
        self,
        operation: str,
        status: str,
        refs: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "operation": operation,
            "status": status,
        }
        entry |= {
            k: str(v)
            for k, v in (refs or {}).items()
            if k in _ID_KEYS and v is not None
        }
        if error_code:
            entry["error"] = error_code
        line = json.dumps(entry) + "\n"
        try:
            with self._lock:
                self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(line)
        except OSError:
            # Losing a log line must not turn a completed write into an error.
            log.exception("Couldn't write the operation log")
