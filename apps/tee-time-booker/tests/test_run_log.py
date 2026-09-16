import csv
from datetime import datetime, timezone

from tee_time_booker.run_log import COLUMNS, RunRecord, log_run


def _record(status: str) -> RunRecord:
    now = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
    return RunRecord(
        run_id="20260920T080000Z",
        created_at=now,
        finished_at=now,
        course_name="Soule Park" if status == "Success" else None,
        tee_time_pst=datetime(2026, 10, 3, 8, 0) if status == "Success" else None,
        cost_dollars=52.0 if status == "Success" else None,
        service_fee_dollars=None,
        status=status,
    )


def test_writes_header_once_and_appends_rows(tmp_path):
    path = tmp_path / "nested" / "log.csv"
    log_run(_record("Success"), path)
    log_run(_record("Error in booking process: Traceback\n  boom"), path)

    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))

    assert list(rows[0].keys()) == COLUMNS
    assert len(rows) == 2
    assert rows[0]["course_name"] == "Soule Park"
    assert rows[0]["cost_dollars"] == "52.0"
    assert rows[1]["course_name"] == ""
    assert "boom" in rows[1]["status"]
