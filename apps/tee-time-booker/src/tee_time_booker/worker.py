"""Carrying out a claimed booking request and recording what came of it.

The CLI and the HTTP API both do the same thing with a request: claim it
(`db.service.claim_request`, in a transaction of its own), drive the site,
then record the outcome against the run and move the request on. This module
is that middle-and-end, in one place, so an outcome means the same thing
whichever way the run was started.

`BookingWorker` runs it in the background for the API: a booking may sleep
for over an hour waiting on a release, far too long for an HTTP response.
"""

import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace

from loguru import logger
from sqlalchemy.orm import Session, sessionmaker

from tee_time_booker.config import Settings
from tee_time_booker.courses import CourseConfig
from tee_time_booker.db import RunStatus
from tee_time_booker.db.service import ClaimedRun, finish_run
from tee_time_booker.models import BookingResult, SearchCriteria
from tee_time_booker.platforms import PaymentOutcomeUnknownError
from tee_time_booker.runner import book_tee_time

# Browsers a single API process drives at once. Runs beyond this are already
# claimed (`running`) and wait their turn in the pool's queue.
MAX_CONCURRENT_RUNS = 4


def describe(course: CourseConfig, criteria: SearchCriteria, run_id: uuid.UUID) -> None:
    logger.info(
        f"Run {run_id}: booking {course.name} ({course.platform}) on "
        f"{criteria.target_date:%A %Y-%m-%d} between "
        f"{criteria.earliest:%I:%M %p} and {criteria.latest:%I:%M %p} "
        f"for {criteria.players} players, {criteria.holes} holes"
    )


def execute(
    sessions: sessionmaker[Session],
    claimed: ClaimedRun,
    settings: Settings,
    *,
    wait_for_release: bool = True,
) -> RunStatus:
    """Drive the site for a claimed request and record the run's outcome.

    Never raises for a booking failure: every outcome, including an exception
    from the adapter, is written to the run and returned as its status. The
    database is touched only at the end, so no connection is held while the
    run waits on a release.
    """
    describe(claimed.course, claimed.criteria, claimed.run_id)
    result: BookingResult | None = None
    status = RunStatus.ERROR
    detail: str | None = None
    try:
        result = book_tee_time(
            claimed.course,
            claimed.criteria,
            settings,
            str(claimed.run_id),
            account_env_prefix=claimed.account_env_prefix,
            wait_for_release=wait_for_release,
        )
        status = RunStatus.SUCCESS if result.submitted else RunStatus.DRY_RUN
    except PaymentOutcomeUnknownError as e:
        status = RunStatus.UNKNOWN
        detail = str(e)
        logger.exception(f"Unknown payment outcome: {detail}")
    except Exception as e:
        # Error messages can embed whole server responses; keep a short
        # message in the database and the traceback in the job log.
        status = RunStatus.ERROR
        detail = brief(e)
        logger.exception(f"Error in booking process: {detail}")
    finally:
        with sessions() as session, session.begin():
            finish_run(session, claimed.run_id, status, result=result, detail=detail)
    return status


def brief(e: Exception, limit: int = 200) -> str:
    """`ExcType: first line of the message`, capped at `limit` characters."""
    message = str(e).splitlines()[0] if str(e) else ""
    text = f"{type(e).__name__}: {message}" if message else type(e).__name__
    return text[:limit]


class BookingWorker:
    """Runs claimed requests on background threads.

    Playwright's sync API can't run on an event-loop thread, and a run may
    take an hour, so each one gets a thread of its own from a small pool. The
    database is the record of what's running; the futures kept here are only
    so a caller (a test, a shutdown) can wait for a run to finish.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        settings: Settings,
        max_workers: int = MAX_CONCURRENT_RUNS,
    ) -> None:
        self.sessions = sessions
        self.settings = settings
        self._pool = ThreadPoolExecutor(max_workers, thread_name_prefix="booking")
        self.futures: dict[uuid.UUID, Future[RunStatus]] = {}

    def start(
        self, claimed: ClaimedRun, *, submit_payment: bool, wait_for_release: bool
    ) -> Future[RunStatus]:
        """Run `claimed` in the background.

        Paying needs both switches on: the operator's `TEE_TIME_SUBMIT_PAYMENT`
        and the caller's `submit_payment`. Either off makes it a dry run.
        """
        settings = self.settings
        if not submit_payment:
            settings = replace(settings, submit_payment=False)
        future = self._pool.submit(
            execute, self.sessions, claimed, settings, wait_for_release=wait_for_release
        )
        self.futures[claimed.run_id] = future
        future.add_done_callback(lambda _: self.futures.pop(claimed.run_id, None))
        return future

    def shutdown(self, wait: bool = False) -> None:
        """Stop taking runs.

        Runs already on a thread finish and record themselves. Runs still in
        the queue were claimed (`running`) when they were accepted, so they
        are recorded as errors here rather than left claimed forever.
        """
        pending = list(self.futures.items())
        self._pool.shutdown(wait=wait, cancel_futures=True)
        for run_id, future in pending:
            if future.cancelled():
                with self.sessions() as session, session.begin():
                    finish_run(
                        session,
                        run_id,
                        RunStatus.ERROR,
                        detail="Cancelled: the API shut down before the run started",
                    )
