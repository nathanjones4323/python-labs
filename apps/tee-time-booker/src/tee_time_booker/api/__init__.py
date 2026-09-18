"""The HTTP API: the CLI's operations, invokable on demand.

    uvicorn --factory tee_time_booker.api:create_app --port 8000

Everything the CLI can do is a route here, over the same `db.service` and
`runner` calls, so the two can't drift. The one structural difference is
that a booking is a *job*: it may sleep an hour for a release and drives a
browser for minutes after, so `book_plan` / `run_request` claim the request,
start it on a `BookingWorker` thread and answer 202 with a run id, and the
caller polls `get_run`. Every route carries an `operation_id`, which is the
tool name an MCP server built from this app exposes (`tee_time_booker.mcp`).

Routes are plain `def`s on purpose: Playwright's sync API can't run on the
event loop thread, and FastAPI runs sync routes on a thread pool.

There is no authentication. Run it where only you can reach it.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker

from tee_time_booker.api.routers import courses, plans, requests, runs
from tee_time_booker.config import Settings
from tee_time_booker.db import InvalidTransition, make_session_factory
from tee_time_booker.db.service import NotFound
from tee_time_booker.worker import BookingWorker


def create_app(
    settings: Settings | None = None,
    sessions: sessionmaker[Session] | None = None,
    worker: BookingWorker | None = None,
) -> FastAPI:
    """The app around `settings` (default: the environment).

    Tests pass their own session factory and worker. The state is set up
    here rather than in the lifespan because an MCP server drives the app
    through an ASGI transport that never runs lifespan events; the engine
    doesn't connect until first use, so nothing happens at creation.
    """
    settings = settings or Settings.from_env()
    sessions = sessions or make_session_factory(settings.require_database_url())
    worker = worker or BookingWorker(sessions, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            worker.shutdown()

    app = FastAPI(
        title="Tee Time Booker",
        version="0.1.0",
        description=(
            "Books tee times on foreUP and GolfNow courses. A booking is a "
            "background run: start one, then poll it."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.sessions = sessions
    app.state.worker = worker
    app.include_router(courses.router)
    app.include_router(plans.router)
    app.include_router(requests.router)
    app.include_router(runs.router)

    # The service layer raises plain exceptions the CLI prints; here each
    # kind is one status code, so routes don't translate them one by one.
    @app.exception_handler(NotFound)
    def _not_found(request: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(InvalidTransition)
    def _conflict(request: Request, exc: InvalidTransition) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    def _unprocessable(request: Request, exc: ValueError) -> JSONResponse:
        # A date outside the booking window, a mismatched account, ...
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(RuntimeError)
    def _upstream(request: Request, exc: RuntimeError) -> JSONResponse:
        # The booking site or the environment let a search down: a login
        # rejected, a missing secret, an API response that wasn't understood.
        return JSONResponse(status_code=502, content={"detail": str(exc)[:500]})

    @app.get("/health", operation_id="health", tags=["health"], summary="Liveness")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
