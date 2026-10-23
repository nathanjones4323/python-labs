"""The shape of a bookable course, and the seed data for the ones we know.

Courses live in the `courses` table; `tee_time_booker.db` turns a row into one
of these dataclasses (the subclass an adapter needs, picked by `platform`) and
back. The constants at the bottom are what `tee_time_booker.seed` loads into
an empty database, and what the tests use as fixtures.

A course says where it is and how its site releases days, plus a description
and a rough green-fee range so a user (or the MCP client acting for one) can
pick a course without opening the site. *How* a user wants to play it
(weekday, window, group size) is a `Preferences` on their booking plan, not a
property of the course.

Adding a course is adding a seed entry. Adding a *site* is adding an adapter
and, if the site needs identifiers these classes don't carry, a subclass.
"""

from dataclasses import dataclass, fields
from datetime import date, time
from typing import Any
from zoneinfo import ZoneInfo


@dataclass(frozen=True, kw_only=True)
class ReleaseSchedule:
    """When a course opens a new day's tee sheet.

    Courses that release at a fixed local time (foreUP's midnight drop) have
    one; sites with rolling availability (GolfNow) leave it unset and book
    whatever is already open.
    """

    # Course-local time of day the sheet opens.
    opens_at: time
    # A run starting at or after this time is waiting for the *coming*
    # release, so it belongs to the day about to begin, not the one ending.
    pre_release_start: time


@dataclass(frozen=True, kw_only=True)
class GreenFees:
    """Roughly what a round costs, read off the site's tee-time listings.

    An estimate to set expectations with, not a quote. Each range runs from
    the cheapest single-player green fee the site listed (typically twilight)
    to the dearest (typically a prime morning slot), before booking fees.
    `holes` is the round the fees are for, and `sampled_on` the day the
    listings were read, so a stale estimate is recognisable as one.
    """

    holes: int
    weekday_low: float
    weekday_high: float
    weekend_low: float
    weekend_high: float
    sampled_on: date


@dataclass(frozen=True, kw_only=True)
class Reviews:
    """What golfers who played it thought, as the site's review summary.

    Reviews keep arriving, so `sampled_on` marks when the summary was read.
    """

    average: float  # stars out of 5
    count: int
    # Reviewers who said they'd recommend the course.
    recommended: int
    sampled_on: date


@dataclass(frozen=True, kw_only=True)
class TeeRating:
    """The USGA course rating and slope from one set of tees.

    Seeds use the longest men's tee, the figure usually quoted as "the"
    course rating. `holes` is the round the rating is for: a 9-hole course
    may be rated as a 9-hole round (half-scale numbers) or as its 18-hole loop.
    """

    tee: str
    holes: int
    par: int
    yardage: int
    rating: float
    slope: int


@dataclass(frozen=True, kw_only=True)
class CourseConfig:
    """A bookable course: where it is and who runs its booking."""

    key: str  # stable identifier, e.g. "soule-park"
    name: str
    platform: str  # adapter name, e.g. "foreup"
    timezone: ZoneInfo
    release: ReleaseSchedule | None
    # How far ahead the site takes bookings, when that's fixed and known: the
    # last bookable day is this many days after the day a run books from
    # (`dates.release_day`). None means the adapter asks the site instead
    # (`BookingPlatform.last_bookable_day`).
    booking_window_days: int | None
    # A few sentences on the course, condensed from the site's facility page.
    description: str | None = None
    green_fees: GreenFees | None = None
    reviews: Reviews | None = None
    tee_rating: TeeRating | None = None

    def platform_config(self) -> dict[str, Any]:
        """The adapter-specific identifiers: every field a subclass adds.

        This is what the `courses.platform_config` JSONB column holds, so a
        new site's identifiers need no schema change.
        """
        base = {f.name for f in fields(CourseConfig)}
        return {
            f.name: getattr(self, f.name) for f in fields(self) if f.name not in base
        }


@dataclass(frozen=True, kw_only=True)
class ForeUpCourse(CourseConfig):
    """A course booked through foreUP's public booking page."""

    # Identifiers from the course's foreUP booking URL and its tee sheet.
    course_id: str
    booking_class: str
    # The label on the booking-class button matching `booking_class`.
    booking_class_name: str
    schedule_id: str


@dataclass(frozen=True, kw_only=True)
class GolfNowCourse(CourseConfig):
    """A course booked through GolfNow."""

    facility_id: int
    # The slug in GolfNow's facility URLs, e.g. "1226-saticoy-regional-golf-course".
    seo_name: str
    # GolfNow's search API wants the facility's coordinates alongside its id.
    latitude: float
    longitude: float


PACIFIC = ZoneInfo("America/Los_Angeles")

# When the GolfNow green-fee ranges below were read: the single-player rates
# each facility listed for the following two weeks, weekdays and weekends
# separated, booking fees excluded. Refresh them by re-reading the listings
# and moving this date.
SAMPLED_ON = date(2026, 9, 17)

# When the GolfNow review summaries and tee ratings below were read, off each
# facility page's embedded model (`averageRating`, `reviewCount`,
# `totalRecommended`, and the `tees` list). The page's headline
# `courseStatistics` is not used: it is missing for some facilities and
# matches none of the current tees for others.
REVIEWS_SAMPLED_ON = date(2026, 10, 22)

SOULE_PARK = ForeUpCourse(
    key="soule-park",
    name="Soule Park",
    platform="foreup",
    timezone=PACIFIC,
    # foreUP opens each day's tee sheet at course-local midnight, 20 days
    # ahead (its "21 day" window counts today).
    release=ReleaseSchedule(opens_at=time(0, 0), pre_release_start=time(20, 0)),
    booking_window_days=20,
    course_id="19620",
    booking_class="4718",
    booking_class_name="Regular",
    schedule_id="1945",
)

SATICOY = GolfNowCourse(
    key="saticoy",
    name="Saticoy Regional Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    # GolfNow releases on a rolling window rather than at a fixed local time.
    release=None,
    # Each GolfNow facility sets its own window (Saticoy lists a week ahead,
    # Sterling Hills three or more), and the site nowhere states it, so the
    # adapter measures it at run time.
    booking_window_days=None,
    description=(
        "A 9-hole, par-34 course of 2,723 yards off Highway 126 on the Ventura "
        "hillsides, with small greens, seventeen bunkers and gently sloping "
        "fairways. Established in 1921 as the Ventura County Country Club to a Max "
        "Behr, George C. Thomas and William P. Bell design; flat enough to walk and "
        "friendly to beginners."
    ),
    green_fees=GreenFees(
        holes=9,
        weekday_low=9.99,
        weekday_high=17.50,
        weekend_low=10.99,
        weekend_high=20.75,
        sampled_on=SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.86,
        count=117,
        recommended=99,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="White/Blue (18-hole)",
        holes=18,
        par=68,
        yardage=5400,
        rating=65.8,
        slope=106,
    ),
    facility_id=1226,
    seo_name="1226-saticoy-regional-golf-course",
    latitude=34.283855,
    longitude=-119.154724,
)

# The other GolfNow courses within ~50 miles of Ventura (SeaBee, on the Navy
# base, and the San Fernando Valley courses are deliberately left out). Same
# rolling release and per-facility window as Saticoy.
OLIVAS_LINKS = GolfNowCourse(
    key="olivas-links",
    name="Olivas Links",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "An 18-hole links-style layout beside the Santa Clara River and Ventura "
        "Harbor, just off the 101 in Ventura and three miles from Buenaventura. The "
        "coastal breeze keeps it cool in summer and mild in winter."
    ),
    # The only weekend rate listed when sampled was $37, so the weekend
    # range is narrower than the course's real spread.
    green_fees=GreenFees(
        holes=18,
        weekday_low=30.00,
        weekday_high=51.00,
        weekend_low=37.00,
        weekend_high=37.00,
        sampled_on=SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.34,
        count=819,
        recommended=782,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Tour",
        holes=18,
        par=72,
        yardage=6818,
        rating=72.7,
        slope=126,
    ),
    facility_id=3585,
    seo_name="3585-olivas-links",
    latitude=34.24058,
    longitude=-119.25286,
)

BUENAVENTURA = GolfNowCourse(
    key="buenaventura",
    name="Buenaventura Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "Ventura's municipal 18, rebuilt in 2005 after a $6.5M re-routing: a par-70 "
        "of 5,826 yards with large bentgrass greens re-sculpted in the classic Bell "
        "style, mature tree-lined fairways and white-sand bunkers. Short enough "
        "that course management matters more than length."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=26.00,
        weekday_high=41.00,
        weekend_low=31.00,
        weekend_high=51.00,
        sampled_on=SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.04,
        count=479,
        recommended=450,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Black",
        holes=18,
        par=70,
        yardage=5942,
        rating=69.1,
        slope=119,
    ),
    facility_id=3586,
    seo_name="3586-buenaventura-golf-course",
    latitude=34.27116,
    longitude=-119.21609,
)

RIVER_RIDGE_VINEYARD = GolfNowCourse(
    key="river-ridge-vineyard",
    name="River Ridge Golf Club - Vineyard Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "The original 18 at River Ridge in Oxnard, a par-72 of 6,777 yards with "
        "country-club conditioning at public prices. Ocean breezes keep it cool; "
        "the facility has 36 holes, an all-grass range and practice greens."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=26.00,
        weekday_high=58.00,
        weekend_low=31.00,
        weekend_high=68.00,
        sampled_on=SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.81,
        count=723,
        recommended=602,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue",
        holes=18,
        par=72,
        yardage=6730,
        rating=72.6,
        slope=129,
    ),
    facility_id=4419,
    seo_name="4419-river-ridge-golf-club-vineyard-course",
    latitude=34.22786,
    longitude=-119.20818,
)

RIVER_RIDGE_VICTORIA_LAKES = GolfNowCourse(
    key="river-ridge-victoria-lakes",
    name="River Ridge Golf Club - Victoria Lakes",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "River Ridge's newer 18 (2008) in Oxnard, a par-72 of up to 7,081 yards "
        "with bent/poa greens and hybrid bermuda fairways. Strategic rather than "
        'long, with a water-lined "Amen Corner" from the par-5 12th through the '
        "par-5 14th; it hosted a U.S. Senior Open qualifier in 2016."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=31.00,
        weekday_high=63.00,
        weekend_low=36.00,
        weekend_high=73.00,
        sampled_on=SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.71,
        count=114,
        recommended=97,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Black",
        holes=18,
        par=72,
        yardage=7081,
        rating=74.0,
        slope=134,
    ),
    facility_id=17149,
    seo_name="17149-river-ridge-golf-club-victoria-lakes",
    latitude=34.22997,
    longitude=-119.20293,
)

STERLING_HILLS = GolfNowCourse(
    key="sterling-hills",
    name="Sterling Hills Golf Club",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A Damian Pascuzzo par-71 (1999) in Camarillo, 6,813 yards from the tips "
        "with six sets of tees, walkable, with views out to the Channel Islands and "
        "a reputation for the best greens in the county. Pre-twilight and twilight "
        "rates include a Pub & Grill voucher and range balls."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=32.00,
        weekday_high=80.00,
        weekend_low=53.00,
        weekend_high=86.00,
        sampled_on=SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.35,
        count=1733,
        recommended=1496,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Gold",
        holes=18,
        par=71,
        yardage=6820,
        rating=73.3,
        slope=133,
    ),
    facility_id=5302,
    seo_name="5302-sterling-hills-golf-club",
    latitude=34.24453,
    longitude=-119.09353,
)

OJAI_VALLEY_INN = GolfNowCourse(
    key="ojai-valley-inn",
    name="Ojai Valley Inn",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "The resort course at the Ojai Valley Inn: a 1923 George C. Thomas Jr. and "
        'Billy Bell design, toughened by Jay Morrish in 1988, with two "lost" '
        "original holes restored in 1999. A par-70 of 6,305 yards with six par-4s "
        "over 400 yards; host of seven Senior PGA Tour events."
    ),
    # Few public slots are listed (a handful a day), so the ranges are
    # thin: one weekday rate, and a weekend twilight discount.
    green_fees=GreenFees(
        holes=18,
        weekday_low=144.46,
        weekday_high=144.46,
        weekend_low=109.99,
        weekend_high=144.46,
        sampled_on=SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.39,
        count=1002,
        recommended=924,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue",
        holes=18,
        par=70,
        yardage=6252,
        rating=71.0,
        slope=131,
    ),
    facility_id=6632,
    seo_name="6632-ojai-valley-inn",
    latitude=34.44584,
    longitude=-119.25294,
)

CAMARILLO_SPRINGS = GolfNowCourse(
    key="camarillo-springs",
    name="Camarillo Springs Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A Ted Robinson par-72 (1971) beside the 101 in Camarillo, 6,375 yards from "
        "the regular tees with water on more than half the holes and a risk-reward "
        "finish (a short par-5 16th over water, then water down the left of the "
        "18th). Also has a Toptracer range and a golf academy."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=40.99,
        weekday_high=54.00,
        weekend_low=53.99,
        weekend_high=67.00,
        sampled_on=SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.01,
        count=1007,
        recommended=637,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Black",
        holes=18,
        par=72,
        yardage=6921,
        rating=73.2,
        slope=132,
    ),
    facility_id=4796,
    seo_name="4796-camarillo-springs-golf-course",
    latitude=34.22739,
    longitude=-118.97625,
)

LOS_ROBLES_GREENS = GolfNowCourse(
    key="los-robles-greens",
    name="Los Robles Greens Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A par-70 municipal course in the Conejo Valley foothills of Thousand Oaks, "
        "900 feet up and 6,137 yards, known for the giant oaks that line its "
        'fairways and guard its undulating greens: "a public course in a private '
        'course setting."'
    ),
    # The only weekend rate listed when sampled was $45, so the weekend
    # range is narrower than the course's real spread.
    green_fees=GreenFees(
        holes=18,
        weekday_low=31.99,
        weekday_high=72.00,
        weekend_low=45.00,
        weekend_high=45.00,
        sampled_on=SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.30,
        count=788,
        recommended=713,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Black",
        holes=18,
        par=70,
        yardage=6304,
        rating=70.3,
        slope=126,
    ),
    facility_id=3565,
    seo_name="3565-los-robles-greens-golf-course",
    latitude=34.17520,
    longitude=-118.87611,
)

TIERRA_REJADA = GolfNowCourse(
    key="tierra-rejada",
    name="Tierra Rejada Golf Club",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A par-72 in Moorpark stretching to 7,015 yards from the back tees, with "
        "well-groomed fairways and greens, wide views and a layout that stays "
        "playable for every skill level."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=35.00,
        weekday_high=105.00,
        weekend_low=60.00,
        weekend_high=132.00,
        sampled_on=SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.40,
        count=930,
        recommended=818,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Professional",
        holes=18,
        par=72,
        yardage=7017,
        rating=73.4,
        slope=132,
    ),
    facility_id=3582,
    seo_name="3582-tierra-rejada-golf-club",
    latitude=34.26899,
    longitude=-118.84688,
)

LAKE_LINDERO = GolfNowCourse(
    key="lake-lindero",
    name="Lake Lindero Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A 9-hole, par-30 executive course of about 1,600 yards at Lindero Country "
        "Club in Agoura Hills: a quick round, with the club's tennis courts, pool "
        "and bar and grill alongside."
    ),
    green_fees=GreenFees(
        holes=9,
        weekday_low=12.99,
        weekday_high=21.00,
        weekend_low=14.99,
        weekend_high=22.00,
        sampled_on=SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.03,
        count=115,
        recommended=95,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue",
        holes=9,
        par=29,
        yardage=1695,
        rating=29.7,
        slope=96,
    ),
    facility_id=8487,
    seo_name="8487-lake-lindero-golf-course",
    latitude=34.15608,
    longitude=-118.78950,
)

SANTA_BARBARA_GC = GolfNowCourse(
    key="santa-barbara-gc",
    name="Santa Barbara Golf Club",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "Santa Barbara's municipal par-70, 18 holes and 6,014 yards with views of "
        "the Santa Ynez foothills and the Pacific. Audubon International certified, "
        "with Mulligan's Café and a full golf shop on site."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=69.99,
        weekday_high=94.00,
        weekend_low=84.99,
        weekend_high=106.00,
        sampled_on=SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.28,
        count=401,
        recommended=358,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue",
        holes=18,
        par=70,
        yardage=6022,
        rating=69.4,
        slope=123,
    ),
    facility_id=6252,
    seo_name="6252-santa-barbara-golf-club",
    latitude=34.44693,
    longitude=-119.74282,
)

GLEN_ANNIE = GolfNowCourse(
    key="glen-annie",
    name="Glen Annie Golf Club",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A Robert Muir Graves and Damian Pascuzzo par-71 (1997) in the foothills "
        "above Goleta, 6,420 yards with four sets of tees, winding through avocado "
        "groves and palms with plenty of elevation change and panoramic views of "
        "the Pacific and Channel Islands. Full practice facilities and the Frog Bar "
        "& Grill."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=58.00,
        weekday_high=102.00,
        weekend_low=60.00,
        weekend_high=113.00,
        sampled_on=SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.39,
        count=295,
        recommended=249,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Green",
        holes=18,
        par=71,
        yardage=6417,
        rating=71.2,
        slope=129,
    ),
    facility_id=15877,
    seo_name="15877-glen-annie-golf-club",
    latitude=34.44385,
    longitude=-119.87483,
)

# Loaded into the `courses` table by `python -m tee_time_booker.seed`.
SEED_COURSES: tuple[CourseConfig, ...] = (
    SOULE_PARK,
    SATICOY,
    OLIVAS_LINKS,
    BUENAVENTURA,
    RIVER_RIDGE_VINEYARD,
    RIVER_RIDGE_VICTORIA_LAKES,
    STERLING_HILLS,
    OJAI_VALLEY_INN,
    CAMARILLO_SPRINGS,
    LOS_ROBLES_GREENS,
    TIERRA_REJADA,
    LAKE_LINDERO,
    SANTA_BARBARA_GC,
    GLEN_ANNIE,
)
