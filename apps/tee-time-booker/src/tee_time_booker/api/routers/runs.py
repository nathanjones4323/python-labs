import uuid

from fastapi import APIRouter, Query

from tee_time_booker.api.deps import Sessions
from tee_time_booker.api.schemas import RunOut
from tee_time_booker.db.service import get_run, recent_runs

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("", operation_id="list_runs", summary="The most recent runs, newest first")
def list_(
    sessions: Sessions, limit: int = Query(default=20, ge=1, le=500)
) -> list[RunOut]:
    with sessions() as session:
        return [RunOut.from_row(r) for r in recent_runs(session, limit)]


@router.get(
    "/{run_id}",
    operation_id="get_run",
    summary="One run: poll this after starting a booking",
)
def get(run_id: uuid.UUID, sessions: Sessions) -> RunOut:
    with sessions() as session:
        return RunOut.from_row(get_run(session, run_id))
