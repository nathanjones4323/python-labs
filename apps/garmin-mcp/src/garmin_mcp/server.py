"""The MCP server: an explicit list of tools over stdio.

    garmin-mcp          # stdio, for Claude Code / Claude Desktop

Each tool validates its arguments (the SDK does that from the type hints),
calls one service method and returns a Pydantic model as structured output.
Failures a caller can act on come back as tool errors whose message leads
with the category (`not_found: ...`, `authentication: ...`) after the SDK's
"Error executing tool <name>: " prefix; see `errors`.

Tools are sync functions; the SDK runs each in a worker thread, so a slow
Garmin call doesn't stall the session.

stdio is the only transport. Serving this over HTTP would put the account
behind whatever auth sits in front of it, so it would need its own
authentication and authorization first; it isn't here.
"""

import argparse
import functools
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from logging.handlers import RotatingFileHandler
from typing import Annotated, Any, ParamSpec, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BeforeValidator, Field, WithJsonSchema

from garmin_mcp.approval import ApprovalBook
from garmin_mcp.auth import GarminSession
from garmin_mcp.config import Settings, get_settings
from garmin_mcp.errors import GarminMcpError
from garmin_mcp.oplog import OperationLog
from garmin_mcp.repositories.failures import REAUTH
from garmin_mcp.repositories.garmin_client import (
    GarminConnectRepository,
    GarminRepository,
)
from garmin_mcp.schemas.activity_sets import MAX_ACTIVITY_SETS, ActivitySet
from garmin_mcp.schemas.calendar import Calendar
from garmin_mcp.schemas.results import (
    AccountStatus,
    ActivityDetails,
    ActivitySummary,
    DevicePushResult,
    DeviceSummary,
    Metric,
    TrainingMetrics,
    WriteResult,
)
from garmin_mcp.schemas.workouts import (
    ExerciseMatch,
    Workout,
    WorkoutSummary,
    WorkoutView,
)
from garmin_mcp.services.activities import ActivityService
from garmin_mcp.services.activity_sets import ActivitySetService
from garmin_mcp.services.calendar import CalendarService
from garmin_mcp.services.devices import DeviceService
from garmin_mcp.services.health import HealthService
from garmin_mcp.services.workouts import WorkoutService
from garmin_mcp.services.writes import WriteRunner

log = logging.getLogger("garmin_mcp")

INSTRUCTIONS = """
Read and write access to one person's Garmin Connect account: activities,
health and training metrics, the workout library, the training calendar,
and sending workouts to their watch.

This server executes; it doesn't plan training. Decide what the workout
should be with the user, then use these tools to put it in Garmin.

Every write tool (create_workout, update_workout, schedule_workout,
reschedule_workout, push_workout_to_device, update_activity_exercise_sets)
works in two calls:
1. Call it without confirmation_token. Nothing changes; you get a preview
   and a token (or status dry_run, when the server is in dry-run mode).
2. Show the preview to the user in plain words. Only after they explicitly
   approve, call the same tool with the same arguments plus the token.
Never approve on the user's behalf, and never reuse a token for a
different change.

If a write returns status "unknown", it may have happened. Don't retry it;
check with the read tools (get_workout, get_workout_calendar,
get_activity_details) and tell the user. Strength exercises must be names
from Garmin's catalog: look them up with search_exercises, and if a name
isn't found, ask the user which catalog exercise they mean rather than
picking one.
""".strip()


def _iso_date(value: Any) -> Any:
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("expected a calendar date as YYYY-MM-DD")
    return value


ISODate = Annotated[
    date,
    BeforeValidator(_iso_date),
    WithJsonSchema(
        {"type": "string", "format": "date", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
    ),
]
PositiveId = Annotated[int, Field(gt=0)]
DryRun = Annotated[
    bool, Field(description="Preview only, even with a confirmation_token.")
]
Token = Annotated[
    str | None,
    Field(
        description="The token from this tool's preview. Pass it only after "
        "the user has approved that preview."
    ),
]

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)


def _write(destructive: bool) -> ToolAnnotations:
    return ToolAnnotations(
        read_only_hint=False,
        destructive_hint=destructive,
        idempotent_hint=False,
        open_world_hint=True,
    )


@dataclass
class App:
    settings: Settings
    session: GarminSession | None
    repo: GarminRepository
    runner: WriteRunner
    workouts: WorkoutService
    calendar: CalendarService
    activities: ActivityService
    activity_sets: ActivitySetService
    health: HealthService
    devices: DeviceService


def build_app(settings: Settings, repo: GarminRepository | None = None) -> App:
    session = None
    if repo is None:
        session = GarminSession(settings)
        repo = GarminConnectRepository(session)
    runner = WriteRunner(
        dry_run=settings.dry_run,
        approvals=ApprovalBook(settings.approval_ttl_seconds),
        oplog=OperationLog(settings.state_dir / "operations.jsonl"),
    )
    workouts = WorkoutService(repo, runner)
    return App(
        settings=settings,
        session=session,
        repo=repo,
        runner=runner,
        workouts=workouts,
        calendar=CalendarService(repo, runner),
        activities=ActivityService(repo),
        activity_sets=ActivitySetService(
            repo,
            runner,
            workouts.resolve_exercise,
            settings.state_dir / "activity-set-backups",
        ),
        health=HealthService(repo),
        devices=DeviceService(repo, runner, settings.default_device_id),
    )


P = ParamSpec("P")
R = TypeVar("R")


def _reported(fn: Callable[P, R]) -> Callable[P, R]:
    """Our errors become tool errors led by their category. Only the tool
    name, timing and outcome are logged: arguments and results can carry
    health data."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        started = time.monotonic()
        try:
            result = fn(*args, **kwargs)
        except GarminMcpError as exc:
            log.info(
                "%s -> %s (%.1fs)", fn.__name__, exc.code, time.monotonic() - started
            )
            raise ToolError(f"{exc.code}: {exc}") from exc
        except Exception:
            log.exception("%s crashed", fn.__name__)
            raise
        status = getattr(result, "status", "ok")
        log.info("%s -> %s (%.1fs)", fn.__name__, status, time.monotonic() - started)
        return result

    return wrapper


def create_server(app: App) -> MCPServer:
    mcp = MCPServer(name="garmin", instructions=INSTRUCTIONS)

    def tool(
        annotations: ToolAnnotations,
    ) -> Callable[[Callable[P, R]], Callable[P, R]]:
        def register(fn: Callable[P, R]) -> Callable[P, R]:
            mcp.add_tool(_reported(fn), annotations=annotations)
            return fn

        return register

    # -- account ----------------------------------------------------------

    @tool(READ)
    def get_account_status() -> AccountStatus:
        """Whether the server can reach Garmin Connect with its saved session,
        and whether writes are enabled. Check this first when other tools
        report authentication or network errors."""
        return account_status(app)

    # -- activities and metrics -------------------------------------------

    @tool(READ)
    def get_recent_activities(
        start_date: ISODate | None = None,
        end_date: ISODate | None = None,
        activity_type: Annotated[
            str | None,
            Field(
                description="Garmin type key, e.g. running, strength_training, cycling."
            ),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> list[ActivitySummary]:
        """Recorded activities, newest first. Defaults to the last 14 days."""
        return app.activities.recent(start_date, end_date, activity_type, limit)

    @tool(READ)
    def get_activity_details(activity_id: PositiveId) -> ActivityDetails:
        """One activity's summary, plus laps (or, for strength, the logged
        sets). No GPS or second-by-second data."""
        return app.activities.details(activity_id)

    @tool(_write(destructive=True))
    def update_activity_exercise_sets(
        activity_id: PositiveId,
        sets: Annotated[
            list[ActivitySet],
            Field(min_length=1, max_length=MAX_ACTIVITY_SETS),
        ],
        dry_run: DryRun = False,
        confirmation_token: Token = None,
    ) -> WriteResult:
        """Replace every set logged on a recorded strength activity (not a
        workout in the library). For fixing what the watch logged: wrong
        exercise, reps or weight, sets to drop. Get the sets with
        get_activity_details, correct them, and pass the whole list back in
        order, rests included. Keep each set's duration_seconds: start times
        are rebuilt by laying the sets end to end from the first one. Two
        calls: preview (old and new, set by set) first, then again with the
        token once the user approves."""
        return app.activity_sets.update_exercise_sets(
            activity_id, sets, dry_run=dry_run, confirmation_token=confirmation_token
        )

    @tool(READ)
    def get_training_metrics(
        start_date: ISODate,
        end_date: ISODate | None = None,
        metrics: Annotated[
            list[Metric] | None,
            Field(description="Defaults to daily_summary, sleep, training_readiness."),
        ] = None,
    ) -> TrainingMetrics:
        """Daily health and training metrics for up to 14 days. Ask only for
        the metrics the question needs: each is a Garmin call per day."""
        return app.health.metrics(start_date, end_date, metrics)

    # -- workouts ---------------------------------------------------------

    @tool(READ)
    def search_exercises(
        term: Annotated[str, Field(min_length=1, max_length=80)],
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> list[ExerciseMatch]:
        """Find strength exercises in Garmin's catalog by name (every word
        must appear). Use the returned name or exercise_id in create_workout."""
        return app.workouts.search_exercises(term, limit)

    @tool(READ)
    def list_workouts(
        sport: Annotated[
            str | None,
            Field(pattern="^(strength|running)$", description="strength or running"),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 30,
    ) -> list[WorkoutSummary]:
        """Workouts in the library, most recently changed first."""
        return app.workouts.list_workouts(sport, limit)

    @tool(READ)
    def get_workout(workout_id: PositiveId) -> WorkoutView:
        """One workout, in the same canonical form create_workout takes (or
        null with the reason, for workouts this server can't represent)."""
        return app.workouts.get_workout(workout_id)

    @tool(_write(destructive=False))
    def create_workout(
        workout: Workout,
        allow_duplicate_name: Annotated[
            bool,
            Field(description="Create it even if a workout with this name exists."),
        ] = False,
        dry_run: DryRun = False,
        confirmation_token: Token = None,
    ) -> WriteResult:
        """Add a strength or running workout to the Garmin library. Two
        calls: preview first, then again with the token once the user
        approves. Returns the new workout_id once verified."""
        return app.workouts.create_workout(
            workout,
            allow_duplicate_name=allow_duplicate_name,
            dry_run=dry_run,
            confirmation_token=confirmation_token,
        )

    @tool(_write(destructive=True))
    def update_workout(
        workout_id: PositiveId,
        workout: Workout,
        dry_run: DryRun = False,
        confirmation_token: Token = None,
    ) -> WriteResult:
        """Replace a workout's whole definition (get it with get_workout,
        edit, pass it back). The workout keeps its id and calendar entries.
        Two calls: preview (a diff) first, then again with the token."""
        return app.workouts.update_workout(
            workout_id, workout, dry_run=dry_run, confirmation_token=confirmation_token
        )

    # -- calendar ---------------------------------------------------------

    @tool(READ)
    def get_workout_calendar(start_date: ISODate, end_date: ISODate) -> Calendar:
        """Scheduled workouts and recorded activities between two dates
        (inclusive, up to 92 days)."""
        return app.calendar.get_calendar(start_date, end_date)

    @tool(_write(destructive=False))
    def schedule_workout(
        workout_id: PositiveId,
        date: ISODate,
        allow_duplicate: Annotated[
            bool, Field(description="Schedule it even if it's already on that day.")
        ] = False,
        dry_run: DryRun = False,
        confirmation_token: Token = None,
    ) -> WriteResult:
        """Put a library workout on the calendar (today or later). Two
        calls: preview first, then again with the token."""
        return app.calendar.schedule_workout(
            workout_id,
            date,
            allow_duplicate=allow_duplicate,
            dry_run=dry_run,
            confirmation_token=confirmation_token,
        )

    @tool(_write(destructive=True))
    def reschedule_workout(
        to_date: ISODate,
        scheduled_workout_id: Annotated[
            int | None,
            Field(
                gt=0, description="The calendar entry's id, from get_workout_calendar."
            ),
        ] = None,
        workout_id: Annotated[
            int | None,
            Field(gt=0, description="With from_date, instead of the entry id."),
        ] = None,
        from_date: ISODate | None = None,
        allow_duplicate: bool = False,
        dry_run: DryRun = False,
        confirmation_token: Token = None,
    ) -> WriteResult:
        """Move a calendar entry to another day. Identify it by
        scheduled_workout_id, or by workout_id plus from_date when that
        workout is on that day exactly once. Two calls, as for the other
        writes."""
        return app.calendar.reschedule_workout(
            to_date,
            scheduled_workout_id=scheduled_workout_id,
            workout_id=workout_id,
            from_date=from_date,
            allow_duplicate=allow_duplicate,
            dry_run=dry_run,
            confirmation_token=confirmation_token,
        )

    # -- devices ----------------------------------------------------------

    @tool(READ)
    def list_devices() -> list[DeviceSummary]:
        """Devices registered to the account, with Garmin's last-used one marked."""
        return app.devices.list_devices()

    @tool(_write(destructive=False))
    def push_workout_to_device(
        workout_id: PositiveId,
        device_id: Annotated[
            int | None,
            Field(
                gt=0,
                description="Defaults to GARMIN_DEFAULT_DEVICE_ID, then the "
                "last-used device.",
            ),
        ] = None,
        dry_run: DryRun = False,
        confirmation_token: Token = None,
    ) -> WriteResult:
        """Queue a library workout for a watch; it arrives at the watch's next
        sync. The result reports upload (queued on Garmin) and sync (taken
        by the watch) separately. Two calls, as for the other writes."""
        return app.devices.push_workout(
            workout_id,
            device_id,
            dry_run=dry_run,
            confirmation_token=confirmation_token,
        )

    @tool(READ)
    def get_device_push_status(
        workout_id: PositiveId, device_id: PositiveId
    ) -> DevicePushResult:
        """Whether a pushed workout is still waiting in Garmin's queue for the
        device (pending_on_garmin) or has left it (not_in_queue)."""
        return app.devices.push_status(workout_id, device_id)

    return mcp


def account_status(app: App) -> AccountStatus:
    settings = app.settings
    status = AccountStatus(
        authenticated=False,
        connected=False,
        token_file=str(settings.token_file),
        token_file_present=settings.token_file.exists(),
        dry_run=settings.dry_run,
    )
    try:
        profile = app.repo.get_profile()
    except GarminMcpError as exc:
        status.error = f"{exc.code}: {exc}"
        # Garmin answered (and refused), as opposed to never being asked.
        status.connected = status.token_file_present and exc.code in (
            "authentication",
            "rate_limit",
            "garmin_api",
        )
        if exc.code == "authentication":
            status.action = REAUTH
        elif exc.code in ("network", "rate_limit"):
            status.action = "Try again in a few minutes."
        return status
    status.authenticated = status.connected = True
    status.display_name = profile.get("display_name")
    status.unit_system = profile.get("unit_system")
    return status


def setup_logging(settings: Settings) -> None:
    settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        settings.state_dir / "garmin-mcp.log", maxBytes=5_000_000, backupCount=3
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s [%(process)d] %(name)s: %(message)s"
        )
    )
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())
    # The library logs request details at DEBUG; keep it to warnings
    # whatever the app's level.
    for noisy in ("garminconnect", "urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> None:
    argparse.ArgumentParser(
        prog="garmin-mcp", description=__doc__.splitlines()[0]
    ).parse_args(argv)
    settings = get_settings()
    setup_logging(settings)
    app = build_app(settings)
    log.info("server starting (stdio), dry_run=%s", settings.dry_run)

    # Restore the session in the background so a slow Garmin doesn't hold
    # up the MCP handshake; a failure is logged and every tool reports it.
    def warm() -> None:
        try:
            app.repo.get_profile()
        except GarminMcpError as exc:
            log.warning("Garmin session not available: %s: %s", exc.code, exc)

    threading.Thread(target=warm, name="garmin-login", daemon=True).start()
    try:
        create_server(app).run()
    finally:
        log.info("server stopped")


if __name__ == "__main__":
    main()
