"""What the API sends and accepts.

Responses are built from the ORM rows explicitly (`from_*` classmethods)
rather than mapped by attribute, so a row's relationships flatten to the
keys and emails a caller can act on and nothing internal leaks by accident.
Field descriptions double as the tool descriptions an MCP client sees, so
they say what a caller needs to decide, not what the column is.
"""

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, Field, WithJsonSchema, model_validator

from tee_time_booker.courses import miles_from_home
from tee_time_booker.db import (
    BookingPlan,
    BookingRequest,
    Course,
    RequestStatus,
    Run,
    RunStatus,
)
from tee_time_booker.models import Preferences, SearchCriteria, TeeTime

# Course-local wall-clock values carry no UTC offset, but JSON Schema's `time`
# and `date-time` formats require one, and an MCP client that checks a tool's
# result against its output schema (Claude Code does) drops the whole result
# over one such value. So these are declared as patterned strings; Pydantic
# parses and sends them exactly as before.
LocalTime = Annotated[
    time,
    WithJsonSchema({"type": "string", "pattern": r"^\d{2}:\d{2}(:\d{2}(\.\d+)?)?$"}),
]
LocalDateTime = Annotated[
    datetime,
    WithJsonSchema(
        {
            "type": "string",
            "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$",
        }
    ),
]

# --- courses ---------------------------------------------------------------


class GreenFeesOut(BaseModel):
    """A rough green-fee range read off the site's listings; not a quote."""

    holes: int = Field(description="The round these fees are for: 9 or 18")
    weekday_low: float = Field(description="Cheapest weekday rate seen, USD")
    weekday_high: float = Field(description="Dearest weekday rate seen, USD")
    weekend_low: float = Field(description="Cheapest weekend rate seen, USD")
    weekend_high: float = Field(description="Dearest weekend rate seen, USD")
    sampled_on: date = Field(
        description="When the listings were read; treat old samples as rough"
    )


class ReviewsOut(BaseModel):
    """The site's summary of golfer reviews."""

    average: float = Field(description="Average review, stars out of 5")
    count: int = Field(description="How many reviews the average is over")
    recommended: int = Field(description="Reviewers who'd recommend the course")
    sampled_on: date = Field(description="When the summary was read")


class TeeRatingOut(BaseModel):
    """USGA course rating and slope from the longest men's tee."""

    tee: str = Field(description="The tee set rated, as the site names it")
    holes: int = Field(
        description="The round rated: 9-hole ratings are roughly half-scale"
    )
    par: int
    yardage: int
    rating: float = Field(
        description="USGA course rating: a scratch golfer's expected score"
    )
    slope: int = Field(
        description=(
            "USGA slope, 55-155: how much harder it plays for a bogey golfer; "
            "113 is average"
        )
    )


class CourseOut(BaseModel):
    key: str = Field(description="Stable identifier, e.g. 'soule-park'")
    name: str
    platform: str = Field(description="Booking site: 'foreup' or 'golfnow'")
    timezone: str
    release_opens_at: LocalTime | None = Field(
        description=(
            "Course-local time a new day's tee sheet opens; "
            "null for rolling availability"
        )
    )
    booking_window_days: int | None = Field(
        description=(
            "Days ahead the site takes bookings, when fixed; "
            "null if the site is asked at run time"
        )
    )
    description: str | None = Field(
        description="A few sentences on the course, when known"
    )
    green_fees: GreenFeesOut | None = Field(
        description=(
            "Estimated single-player green fees before booking fees, when "
            "known; null means no estimate, not free"
        )
    )
    reviews: ReviewsOut | None = Field(
        description="Golfer review summary from the booking site, when known"
    )
    tee_rating: TeeRatingOut | None = Field(
        description="USGA course rating and slope, when known"
    )
    miles_from_home: float | None = Field(
        description=(
            "Straight-line miles from Ventura, CA, where searches are centered; "
            "null if the course's location isn't known"
        )
    )

    @classmethod
    def from_row(cls, course: Course) -> "CourseOut":
        miles = miles_from_home(course.latitude, course.longitude)
        return cls(
            key=course.key,
            name=course.name,
            platform=course.platform,
            timezone=course.timezone,
            release_opens_at=course.release_opens_at,
            booking_window_days=course.booking_window_days,
            description=course.description,
            green_fees=(
                GreenFeesOut(**course.green_fees) if course.green_fees else None
            ),
            reviews=ReviewsOut(**course.reviews) if course.reviews else None,
            tee_rating=(
                TeeRatingOut(**course.tee_rating) if course.tee_rating else None
            ),
            miles_from_home=None if miles is None else round(miles, 1),
        )


# --- plans -----------------------------------------------------------------


class WindowIn(BaseModel):
    """A time window and a group size: what a search and a plan have in common."""

    earliest: LocalTime = Field(
        description="Earliest acceptable tee-off, course-local, inclusive"
    )
    latest: LocalTime = Field(
        description="Latest acceptable tee-off, course-local, inclusive"
    )
    holes: int = Field(ge=1)
    players: int = Field(ge=1, le=4)
    prefer_earlier: bool = Field(
        default=True,
        description="Take the earliest slot in the window; false takes the latest",
    )

    @model_validator(mode="after")
    def _window_is_ordered(self) -> "WindowIn":
        if self.latest < self.earliest:
            raise ValueError("latest must not be before earliest")
        return self


class PreferencesIn(WindowIn):
    """A standing wish: which day, which window, how big a group."""

    target_weekday: int = Field(ge=0, le=6, description="Monday=0 ... Sunday=6")
    weeks_ahead: int = Field(
        ge=1,
        description=(
            "Book the Nth upcoming occurrence of the weekday; "
            "today counts if it matches"
        ),
    )

    def preferences(self) -> Preferences:
        # `include` so a subclass's extra fields (PlanIn) stay out.
        return Preferences(**self.model_dump(include=set(PreferencesIn.model_fields)))


ENV_PREFIX_DESCRIPTION = (
    "Which of the user's site accounts signs in, by its env prefix (e.g. "
    "'SOULE'). Leave it out to use the user's only account on the course's site."
)
AUTO_BOOK_DESCRIPTION = (
    "Let the scheduler book this as soon as the day can be booked, paying "
    "without asking again (payment also needs the server's "
    "TEE_TIME_SUBMIT_PAYMENT switch on). Only set it when the person has "
    "asked to have it booked automatically."
)
FIRST_ATTEMPT_DESCRIPTION = (
    "When the scheduler starts trying to book the date (it may already have "
    "passed); null unless auto-booked"
)


class PlanIn(PreferencesIn):
    user_email: str = Field(
        description="Who the plan belongs to; the user must already exist"
    )
    name: str = Field(
        max_length=200,
        description="Unique per user; posting an existing name updates that plan",
    )
    course: str = Field(description="A course key from the courses listing")
    env_prefix: str | None = Field(default=None, description=ENV_PREFIX_DESCRIPTION)
    auto_book: bool = Field(
        default=False,
        description=(
            AUTO_BOOK_DESCRIPTION
            + " Books each next date in turn, every week. Updating a plan "
            "without it turns auto-booking off."
        ),
    )


class PlanOut(PreferencesIn):
    id: uuid.UUID
    user_email: str
    name: str
    course: str = Field(description="Course key")
    env_prefix: str
    active: bool
    auto_book: bool = Field(
        description="Booked automatically by the scheduler, without asking"
    )
    next_target_date: date = Field(
        description="The date this plan would book if run now, in course-local time"
    )
    first_attempt_at: datetime | None = Field(description=FIRST_ATTEMPT_DESCRIPTION)

    @classmethod
    def from_row(
        cls,
        plan: BookingPlan,
        next_target_date: date,
        first_attempt_at: datetime | None = None,
    ) -> "PlanOut":
        return cls(
            id=plan.id,
            user_email=plan.user.email,
            name=plan.name,
            course=plan.course.key,
            env_prefix=plan.site_account.env_prefix,
            active=plan.active,
            auto_book=plan.auto_book,
            target_weekday=plan.target_weekday,
            weeks_ahead=plan.weeks_ahead,
            earliest=plan.earliest,
            latest=plan.latest,
            holes=plan.holes,
            players=plan.players,
            prefer_earlier=plan.prefer_earlier,
            next_target_date=next_target_date,
            first_attempt_at=first_attempt_at,
        )


class AutoBookIn(BaseModel):
    enabled: bool = Field(
        description=(
            "True arms the plan: the scheduler books each next date without "
            "asking, paying when the server's switch is on. False pauses it."
        )
    )


# --- tee times -------------------------------------------------------------


class TeeTimeOut(BaseModel):
    start: LocalDateTime = Field(
        description="Tee-off, course-local wall time (no offset)"
    )
    holes: int | None
    players: int | None = Field(description="Most players this slot can seat")
    green_fee: float | None = Field(description="Per player, before fees")
    service_fee: float | None
    side: str | None = Field(description="'Front' / 'Back' where the site says")

    @classmethod
    def from_tee_time(cls, t: TeeTime) -> "TeeTimeOut":
        return cls(
            start=t.start,
            holes=t.holes,
            players=t.players,
            green_fee=t.green_fee,
            service_fee=t.service_fee,
            side=t.side,
        )


class FoundTeeTimeOut(TeeTimeOut):
    course: str = Field(description="Course key")
    course_name: str


class TeeTimeSearchOut(BaseModel):
    tee_times: list[FoundTeeTimeOut] = Field(
        description="Every slot in the window at every course searched, earliest first"
    )
    searched: list[str] = Field(description="Keys of the courses searched")
    not_searched: dict[str, str] = Field(
        description=(
            "Courses that couldn't be searched, by key, and why (no account on "
            "that site, a sign-in that failed, ...)"
        )
    )


# --- requests --------------------------------------------------------------


class CriteriaIn(WindowIn):
    """One day's search, for a one-off request with no plan behind it."""

    target_date: date

    def criteria(self) -> SearchCriteria:
        return SearchCriteria(**self.model_dump(include=set(CriteriaIn.model_fields)))


class RequestIn(CriteriaIn):
    user_email: str
    course: str = Field(description="A course key from the courses listing")
    env_prefix: str | None = Field(default=None, description=ENV_PREFIX_DESCRIPTION)
    auto_book: bool = Field(default=False, description=AUTO_BOOK_DESCRIPTION)


class RequestOut(BaseModel):
    id: uuid.UUID
    user_email: str
    course: str = Field(description="Course key")
    plan_id: uuid.UUID | None
    target_date: date
    earliest: LocalTime
    latest: LocalTime
    holes: int
    players: int
    prefer_earlier: bool
    auto_book: bool = Field(
        description=(
            "Booked automatically by the scheduler: this one-off's own flag, or "
            "its plan's"
        )
    )
    first_attempt_at: datetime | None = Field(description=FIRST_ATTEMPT_DESCRIPTION)
    status: RequestStatus = Field(
        description=(
            "pending: waiting to be run. running: a run is on the site now. "
            "booked: paid and confirmed. failed: the last run errored, may be run "
            "again. "
            "unknown: payment was sent and no confirmation seen — check the site "
            "before running again. cancelled."
        )
    )
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(
        cls, request: BookingRequest, first_attempt_at: datetime | None = None
    ) -> "RequestOut":
        return cls(
            id=request.id,
            user_email=request.user.email,
            course=request.course.key,
            plan_id=request.plan_id,
            target_date=request.target_date,
            earliest=request.earliest,
            latest=request.latest,
            holes=request.holes,
            players=request.players,
            prefer_earlier=request.prefer_earlier,
            auto_book=request.armed,
            first_attempt_at=first_attempt_at,
            status=request.status,
            created_at=request.created_at,
            updated_at=request.updated_at,
        )


# --- runs ------------------------------------------------------------------


class RunOptions(BaseModel):
    submit_payment: bool = Field(
        default=False,
        description=(
            "Click the final submit and pay. False stops at checkout (a dry run) "
            "and leaves the request pending. Payment also needs the server's "
            "TEE_TIME_SUBMIT_PAYMENT switch on."
        ),
    )
    wait_for_release: bool = Field(
        default=True,
        description=(
            "If the course releases days at a fixed time and the target's release "
            "is due tonight, wait for it (up to 95 min). False books a day that is "
            "already open, immediately."
        ),
    )


class BookIn(RunOptions):
    target_date: date | None = Field(
        default=None,
        description=(
            "Book this date instead of the plan's computed one. Setting it also "
            "turns wait_for_release off unless given explicitly, since it points "
            "at an already-open day."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _explicit_date_means_no_wait(cls, data):
        if isinstance(data, dict) and data.get("target_date") is not None:
            data.setdefault("wait_for_release", False)
        return data


class RunOut(BaseModel):
    id: uuid.UUID
    request_id: uuid.UUID
    status: RunStatus = Field(
        description=(
            "running: still on the site, poll again. success: booked and paid. "
            "dry_run: reached checkout, nothing booked. error: see detail. "
            "unknown: submit was clicked and no confirmation or error appeared — "
            "check the booking site before retrying. not_open: the day isn't in "
            "the site's booking window yet; the request stays pending."
        )
    )
    scheduled: bool = Field(description="Started by the scheduler, not by a person")
    started_at: datetime
    finished_at: datetime | None
    detail: str | None = Field(description="Short error message, when the run failed")
    tee_time_local: LocalDateTime | None = Field(
        description="The slot held, course-local"
    )
    side: str | None
    green_fee: Decimal | None
    service_fee: Decimal | None
    confirmation: str | None = Field(
        description="The site's confirmation after a real booking"
    )

    @classmethod
    def from_row(cls, run: Run) -> "RunOut":
        return cls(
            id=run.id,
            request_id=run.request_id,
            status=run.status,
            scheduled=run.scheduled,
            started_at=run.started_at,
            finished_at=run.finished_at,
            detail=run.detail,
            tee_time_local=run.tee_time_local,
            side=run.side,
            green_fee=run.green_fee,
            service_fee=run.service_fee,
            confirmation=run.confirmation,
        )


class RunStarted(BaseModel):
    """A run accepted for the background: poll `GET /runs/{run_id}` for the outcome."""

    run_id: uuid.UUID
    request_id: uuid.UUID
    course: str
    target_date: date
    dry_run: bool = Field(description="True when this run will stop before paying")
    wait_for_release: bool
