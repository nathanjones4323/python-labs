"""Append-only CSV log of booking runs."""

import csv
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path


@dataclass
class RunRecord:
    run_id: str
    created_at: datetime
    finished_at: datetime
    course_name: str | None
    tee_time_pst: datetime | None
    cost_dollars: float | None
    service_fee_dollars: float | None
    status: str


COLUMNS = [f.name for f in fields(RunRecord)]


def log_run(record: RunRecord, log_path: Path) -> None:
    """Append one row, writing the header first if the file is new or empty."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not log_path.exists() or log_path.stat().st_size == 0
    with log_path.open("a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(COLUMNS)
        writer.writerow(
            [
                "" if getattr(record, name) is None else getattr(record, name)
                for name in COLUMNS
            ]
        )
