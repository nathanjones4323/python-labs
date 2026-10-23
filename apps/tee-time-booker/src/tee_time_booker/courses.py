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
from math import asin, cos, radians, sin, sqrt
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
    # Where the course is, for searching the ones near `HOME`. Unknown for a
    # course added without them, which every search then includes.
    latitude: float | None = None
    longitude: float | None = None

    def miles_from_home(self) -> float | None:
        """How far the course is from `HOME`, if its location is known."""
        return miles_from_home(self.latitude, self.longitude)

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
    # GolfNow's search API wants the facility's coordinates alongside its id,
    # so here they're required.
    latitude: float
    longitude: float


EARTH_RADIUS_MILES = 3958.8


def miles_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in miles between two (latitude, longitude) points."""
    lat1, lon1, lat2, lon2 = map(radians, (*a, *b))
    h = (
        sin((lat2 - lat1) / 2) ** 2
        + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * asin(sqrt(h))


# Ventura, CA: where a search is centered unless it asks to reach farther.
HOME = (34.2805, -119.2945)


def miles_from_home(latitude: float | None, longitude: float | None) -> float | None:
    """How far a point is from `HOME`, or None when it isn't known."""
    if latitude is None or longitude is None:
        return None
    return miles_between((latitude, longitude), HOME)


# A search with no radius of its own covers this far from `HOME`: Ventura
# County, Ojai, the Conejo Valley and Santa Barbara (out to Glen Annie, 35
# miles), but not the San Fernando Valley and Los Angeles (45 miles and up).
DEFAULT_SEARCH_MILES = 40
# The widest search: every seeded course, out to Los Angeles, Pasadena, Long
# Beach and Solvang.
MAX_SEARCH_MILES = 75

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
    # From OpenStreetMap; foreUP doesn't say.
    latitude=34.44280,
    longitude=-119.23176,
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

# The other GolfNow courses within ~50 miles of Ventura, by way of the 101 and
# Santa Barbara (SeaBee, on the Navy base, is deliberately left out). Same
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

# Every other GolfNow facility within 75 miles of Ventura, by GolfNow's own
# distance search (San Fernando Valley, Los Angeles, Pasadena, the South Bay,
# Long Beach, Santa Clarita, the Antelope Valley and Solvang), nearest first.
# Their green fees were read the same way as above, for the two weeks from
# the day after `WIDER_SAMPLED_ON`. Tee ratings follow the same rule, with a
# 27-hole facility rated by its longest 18-hole combination; short courses
# GolfNow lists no USGA rating for have no `tee_rating`.
WIDER_SAMPLED_ON = date(2026, 10, 22)

KNOLLWOOD = GolfNowCourse(
    key="knollwood",
    name="Knollwood Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A par-72 in Granada Hills (1957), 6,385 yards from the Blue tees, with "
        "colorful foliage, a varied layout and challenging greens, starting with "
        "one of the toughest opening holes in Southern California."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=30.99,
        weekday_high=61.25,
        weekend_low=37.75,
        weekend_high=69.50,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.47,
        count=732,
        recommended=516,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue",
        holes=18,
        par=72,
        yardage=6385,
        rating=70.8,
        slope=124,
    ),
    facility_id=743,
    seo_name="743-knollwood-golf-course",
    latitude=34.28936,
    longitude=-118.49982,
)

BALBOA = GolfNowCourse(
    key="balboa",
    name="Balboa Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "One of the two 18s at the Sepulveda Golf Complex in Encino, a William P. "
        "Bell & Sons par-72 of 6,628 yards that opened in 1954 as the Sepulveda "
        "North Course. Narrow, tree-lined fairways test the shot-making of mid to "
        "high handicappers."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=20.00,
        weekday_high=41.00,
        weekend_low=25.00,
        weekend_high=52.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.59,
        count=447,
        recommended=363,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Black",
        holes=18,
        par=72,
        yardage=6628,
        rating=70.5,
        slope=116,
    ),
    facility_id=12197,
    seo_name="12197-balboa-golf-course",
    latitude=34.172806,
    longitude=-118.4985,
)

ENCINO = GolfNowCourse(
    key="encino",
    name="Encino Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "The other 18 at the Sepulveda Golf Complex, a William P. Bell & Sons "
        "par-72 (1957) stretching to 7,023 yards. Long and open: a test for long "
        "hitters and a forgiving place for mid to high handicappers to work on "
        "their game."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=20.00,
        weekday_high=41.00,
        weekend_low=25.00,
        weekend_high=52.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.96,
        count=476,
        recommended=413,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Black",
        holes=18,
        par=72,
        yardage=7023,
        rating=71.8,
        slope=118,
    ),
    facility_id=12200,
    seo_name="12200-encino-golf-course",
    latitude=34.172806,
    longitude=-118.4985,
)

VAN_NUYS_PAR_3 = GolfNowCourse(
    key="van-nuys-par-3",
    name="Van Nuys Golf Course - 18-Hole Par-3",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "An 18-hole par-3 course (par 54, about 2,200 yards) at Van Nuys Golf "
        "Course, just south of Van Nuys Airport, beside the facility's 9-hole "
        "executive course, putting greens and driving range. Good for practicing "
        "short irons."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=16.99,
        weekday_high=25.00,
        weekend_low=19.99,
        weekend_high=27.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.10,
        count=476,
        recommended=403,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    facility_id=9403,
    seo_name="9403-van-nuys-golf-course-18-hole-par-3",
    latitude=34.201717,
    longitude=-118.491974,
)

VAN_NUYS_EXECUTIVE = GolfNowCourse(
    key="van-nuys-executive",
    name="Van Nuys Golf Course - 9-Hole Executive",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A 9-hole, par-30 executive course of 1,569 yards (1968) just south of Van "
        "Nuys Airport, with three par-4s and views of the planes. Next to the "
        "facility's 18-hole par-3, with putting greens and a driving range; suits "
        "beginners and anyone working on their irons."
    ),
    green_fees=GreenFees(
        holes=9,
        weekday_low=13.99,
        weekday_high=17.00,
        weekend_low=15.99,
        weekend_high=22.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.67,
        count=6,
        recommended=6,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Back",
        holes=9,
        par=30,
        yardage=1569,
        rating=28.2,
        slope=81,
    ),
    facility_id=9404,
    seo_name="9404-van-nuys-golf-course-9-hole-executive",
    latitude=34.201717,
    longitude=-118.491974,
)

WOODLEY_LAKES = GolfNowCourse(
    key="woodley-lakes",
    name="Woodley Lakes Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A Los Angeles municipal par-72 in Van Nuys, designed by Ray Goates and "
        "opened in 1976, playing 6,816 yards from the back tees."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=19.00,
        weekday_high=39.00,
        weekend_low=24.00,
        weekend_high=50.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.04,
        count=610,
        recommended=527,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Black",
        holes=18,
        par=72,
        yardage=6816,
        rating=71.8,
        slope=120,
    ),
    facility_id=12205,
    seo_name="12205-woodley-lakes-golf-course",
    latitude=34.186497,
    longitude=-118.486664,
)

EL_CARISO = GolfNowCourse(
    key="el-cariso",
    name="El Cariso Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A mid-length 18 in Sylmar off the 210, par 62 and 4,493 yards from the "
        "Blue tees, with mountain views and lakes; suits every level. Pro shop, "
        "lessons, putting green and snack bar."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=15.50,
        weekday_high=34.99,
        weekend_low=17.50,
        weekend_high=40.75,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.00,
        count=533,
        recommended=452,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue",
        holes=18,
        par=62,
        yardage=4493,
        rating=61.1,
        slope=98,
    ),
    facility_id=13017,
    seo_name="13017-el-cariso-golf-course",
    latitude=34.314487,
    longitude=-118.41574,
)

HEROES = GolfNowCourse(
    key="heroes",
    name="Heroes Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A 9-hole par-3 of 1,159 yards on the north campus of the West Los Angeles "
        "VA Medical Center, built in 1946 on land given as a refuge for wounded "
        "veterans and run since 2008 by the Bandini Foundation for veterans' "
        "therapy. Holes of 99 to 181 yards, each named for a general or admiral, "
        "off matted tees; aimed at beginners, seniors and short-game practice."
    ),
    green_fees=GreenFees(
        holes=9,
        weekday_low=16.99,
        weekday_high=25.00,
        weekend_low=17.99,
        weekend_high=25.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.36,
        count=39,
        recommended=31,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    facility_id=15765,
    seo_name="15765-heroes-golf-course",
    latitude=34.06396,
    longitude=-118.46395,
)

PENMAR = GolfNowCourse(
    key="penmar",
    name="Penmar Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A 9-hole executive course a few blocks from Venice Beach, par 33 and 2,582 "
        "yards with six par-4s and three par-3s, so approach shots decide the "
        'score. Walkable, and named "Best Nine Hole Golf Course" by Los Angeles '
        "Magazine."
    ),
    green_fees=GreenFees(
        holes=9,
        weekday_low=15.00,
        weekday_high=21.00,
        weekend_low=18.00,
        weekend_high=26.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.88,
        count=289,
        recommended=260,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue",
        holes=9,
        par=33,
        yardage=2582,
        rating=31.9,
        slope=103,
    ),
    facility_id=12219,
    seo_name="12219-penmar-golf-course",
    latitude=34.006516,
    longitude=-118.45924,
)

HANSEN_DAM = GolfNowCourse(
    key="hansen-dam",
    name="Hansen Dam Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A Los Angeles municipal par-72 in Pacoima, built by the city's Recreation "
        "and Parks department and opened in 1977; 6,801 yards from the back tees."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=19.00,
        weekday_high=41.99,
        weekend_low=24.00,
        weekend_high=50.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.19,
        count=592,
        recommended=553,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Black",
        holes=18,
        par=72,
        yardage=6801,
        rating=72.1,
        slope=122,
    ),
    facility_id=12201,
    seo_name="12201-hansen-dam-golf-course",
    latitude=34.25867,
    longitude=-118.390915,
)

SAND_CANYON = GolfNowCourse(
    key="sand-canyon",
    name="Sand Canyon Country Club",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "Formerly Robinson Ranch: a 27-hole desert course in Santa Clarita at the "
        "edge of the Angeles National Forest, reopened to the public under new "
        "ownership after drought and the Sand Canyon Fire. Its three nines play as "
        "18-hole combinations of par 71-72, up to 6,903 yards (Valley/Desert)."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=37.99,
        weekday_high=120.00,
        weekend_low=50.00,
        weekend_high=120.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.11,
        count=28,
        recommended=14,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Black (Valley/Desert)",
        holes=18,
        par=72,
        yardage=6903,
        rating=74.9,
        slope=145,
    ),
    facility_id=222,
    seo_name="222-sand-canyon-country-club",
    latitude=34.411533,
    longitude=-118.405,
)

RANCHO_PARK = GolfNowCourse(
    key="rancho-park",
    name="Rancho Park Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A William P. Bell and William H. Johnson par-71 (1947) in the middle of "
        "Los Angeles, 6,839 yards from the Black tees. Undulating and demanding, it "
        "has hosted numerous LA Opens, the PGA Senior Open and LPGA events."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=22.00,
        weekday_high=31.00,
        weekend_low=27.00,
        weekend_high=56.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.98,
        count=436,
        recommended=390,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Black",
        holes=18,
        par=71,
        yardage=6839,
        rating=72.8,
        slope=130,
    ),
    facility_id=12203,
    seo_name="12203-rancho-park-golf-course",
    latitude=34.044518,
    longitude=-118.412155,
)

RANCHO_PARK_PAR_3 = GolfNowCourse(
    key="rancho-park-par-3",
    name="Rancho Park Golf Course - Par 3",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A 9-hole par-3 beside the Rancho Park championship course, 992 yards of "
        "tree-lined holes from 82 to 148 yards; good for families or short-game "
        "practice. A FootGolf course shares the same ground."
    ),
    green_fees=GreenFees(
        holes=9,
        weekday_low=8.99,
        weekday_high=14.00,
        weekend_low=11.99,
        weekend_high=16.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.37,
        count=194,
        recommended=159,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    facility_id=17155,
    seo_name="17155-rancho-park-golf-course-par-3",
    latitude=34.04251,
    longitude=-118.41193,
)

ALISAL_RIVER = GolfNowCourse(
    key="alisal-river",
    name="River Course at the Alisal",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "One of two courses at the Alisal Ranch in Solvang, about 30 minutes north "
        "of Santa Barbara: a Jack Daray Jr. and Stephen Halsey par-72 (1992) of "
        "6,830 yards with views of the Santa Ynez Mountains. Generous fairways "
        "framed by mounding, elevated tees, plenty of bunkers and undulating, "
        "sloping greens; challenging but not punishing."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=40.00,
        weekday_high=139.00,
        weekend_low=54.00,
        weekend_high=172.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.50,
        count=508,
        recommended=474,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Gold",
        holes=18,
        par=72,
        yardage=6830,
        rating=72.7,
        slope=125,
    ),
    facility_id=8377,
    seo_name="8377-river-course-at-the-alisal",
    latitude=34.63212,
    longitude=-120.116585,
)

ANGELES_NATIONAL = GolfNowCourse(
    key="angeles-national",
    name="Angeles National Golf Club",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A Nicklaus Design desert-links layout (2004) in Sunland at the edge of the "
        "Angeles National Forest, par 72 and 7,141 yards from the Black tees, with "
        "no housing, lakes and a creek in play, and the mountains as a backdrop. A "
        "tough test for good players."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=120.00,
        weekday_high=180.00,
        weekend_low=188.99,
        weekend_high=205.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.12,
        count=1334,
        recommended=974,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Black",
        holes=18,
        par=72,
        yardage=7141,
        rating=74.7,
        slope=143,
    ),
    facility_id=880,
    seo_name="880-angeles-national-golf-club",
    latitude=34.269012,
    longitude=-118.32958,
)

WESTCHESTER = GolfNowCourse(
    key="westchester",
    name="Westchester Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "An 18-hole, par-64 course of 4,339 yards just north of LAX: casual and "
        "affordable, with good greens, night golf, a short-game practice area and a "
        "driving range open until 10pm."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=26.00,
        weekday_high=63.00,
        weekend_low=31.00,
        weekend_high=73.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.72,
        count=517,
        recommended=398,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue",
        holes=18,
        par=64,
        yardage=4339,
        rating=60.6,
        slope=95,
    ),
    facility_id=941,
    seo_name="941-westchester-golf-course",
    latitude=33.95971,
    longitude=-118.412636,
)

DEBELL = GolfNowCourse(
    key="debell",
    name="DeBell Golf Club",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "Burbank's municipal 18 in the foothills of the Verdugo Mountains, a "
        "William F. Bell par-71 (1959) of 5,608 yards from the back tees, with big "
        "views and a long history as a film location. The facility adds a 9-hole "
        "par-3 and a disc golf course."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=27.00,
        weekday_high=57.00,
        weekend_low=31.00,
        weekend_high=67.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.77,
        count=1146,
        recommended=967,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue",
        holes=18,
        par=71,
        yardage=5608,
        rating=68.9,
        slope=122,
    ),
    facility_id=4498,
    seo_name="4498-debell-golf-club",
    latitude=34.20662,
    longitude=-118.304596,
)

DEBELL_PAR_3 = GolfNowCourse(
    key="debell-par-3",
    name="DeBell Par 3 Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A 9-hole par-3 just south of the DeBell 18 in Burbank, with holes of 60 to "
        "90 yards for beginners, juniors and short-game work; disc golfers share "
        "the holes. Open 8am to 5pm."
    ),
    green_fees=GreenFees(
        holes=9,
        weekday_low=4.99,
        weekday_high=5.00,
        weekend_low=6.99,
        weekend_high=7.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=1.14,
        count=35,
        recommended=10,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    facility_id=9448,
    seo_name="9448-debell-par-3-course",
    latitude=34.20567,
    longitude=-118.30524,
)

LAKES_AT_EL_SEGUNDO = GolfNowCourse(
    key="lakes-at-el-segundo",
    name="The Lakes at El Segundo",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A Mike Angus short course in El Segundo run by Topgolf and renovated in "
        "2022: par 30 over 1,060 yards, with night golf and a Toptracer range. "
        "Welcoming to beginners."
    ),
    green_fees=GreenFees(
        holes=9,
        weekday_low=22.99,
        weekday_high=40.00,
        weekend_low=31.99,
        weekend_high=40.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.44,
        count=629,
        recommended=582,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    facility_id=10318,
    seo_name="10318-the-lakes-at-el-segundo",
    latitude=33.91165,
    longitude=-118.39483,
)

ROOSEVELT = GolfNowCourse(
    key="roosevelt",
    name="Roosevelt Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A Los Angeles municipal 9-hole executive course, par 33 and 2,478 yards."
    ),
    green_fees=GreenFees(
        holes=9,
        weekday_low=15.00,
        weekday_high=21.00,
        weekend_low=18.00,
        weekend_high=29.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.06,
        count=428,
        recommended=378,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Black (18-hole)",
        holes=18,
        par=66,
        yardage=4992,
        rating=63.8,
        slope=106,
    ),
    facility_id=12220,
    seo_name="12220-roosevelt-golf-course",
    latitude=34.117706,
    longitude=-118.29317,
)

WESTDRIFT_MANHATTAN_BEACH = GolfNowCourse(
    key="westdrift-manhattan-beach",
    name="Westdrift Manhattan Beach Golf Club",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A 9-hole par-3 of about 1,200 yards at the Westdrift hotel in Manhattan "
        "Beach, with a pro shop, putting green and the Golf Shack patio for food "
        "and drinks; leagues, lessons and junior camps."
    ),
    green_fees=GreenFees(
        holes=9,
        weekday_low=25.99,
        weekday_high=40.00,
        weekend_low=31.99,
        weekend_high=40.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.49,
        count=406,
        recommended=375,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    facility_id=7026,
    seo_name="7026-westdrift-manhattan-beach-golf-club",
    latitude=33.889904,
    longitude=-118.40173,
)

HARDING = GolfNowCourse(
    key="harding",
    name="Harding Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "Griffith Park's other 18 from the early 1920s, a par-72 of 6,714 yards "
        "from the Black tees: narrower than Wilson and tree-lined as it winds "
        "through the park's hills, so placement and strategy count. Peacocks and "
        "deer are common."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=20.00,
        weekday_high=59.99,
        weekend_low=25.00,
        weekend_high=52.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.95,
        count=352,
        recommended=314,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Black",
        holes=18,
        par=72,
        yardage=6714,
        rating=71.4,
        slope=121,
    ),
    facility_id=12202,
    seo_name="12202-harding-golf-course",
    latitude=34.132816,
    longitude=-118.28361,
)

WILSON = GolfNowCourse(
    key="wilson",
    name="Wilson Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "The longer of Griffith Park's two 18s and the longest L.A. city course: a "
        "George C. Thomas Jr. par-72 (1923) of 7,002 yards from the Black tees, "
        "with wide, gently sloping fairways in a tree-lined, semi-forest setting. "
        "Peacocks and deer are common."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=22.00,
        weekday_high=50.99,
        weekend_low=26.00,
        weekend_high=55.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.73,
        count=493,
        recommended=436,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Black",
        holes=18,
        par=72,
        yardage=7002,
        rating=73.2,
        slope=126,
    ),
    facility_id=12204,
    seo_name="12204-wilson-golf-course",
    latitude=34.12356,
    longitude=-118.28784,
)

ALONDRA_PARK_PAR_3 = GolfNowCourse(
    key="alondra-park-par-3",
    name="Alondra Park Par 3 Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "The 18-hole par-3 at Alondra Park in Lawndale, 2,252 yards with small, "
        "elevated greens: one of the more demanding par-3 layouts around and good "
        "short-game practice. Run by LA County Parks beside the park's regulation "
        "18."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=8.50,
        weekday_high=18.50,
        weekend_low=9.99,
        weekend_high=18.50,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.19,
        count=359,
        recommended=303,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="White",
        holes=18,
        par=54,
        yardage=2252,
        rating=52.3,
        slope=96,
    ),
    facility_id=9546,
    seo_name="9546-alondra-park-par-3-golf-course",
    latitude=33.88864,
    longitude=-118.34898,
)

LOS_FELIZ = GolfNowCourse(
    key="los-feliz",
    name="Los Feliz Municipal Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A Los Angeles municipal 9-hole par-3 of about 1,000 yards in Los Feliz, "
        "for a quick round or short-game practice."
    ),
    green_fees=GreenFees(
        holes=9,
        weekday_low=8.99,
        weekday_high=13.00,
        weekend_low=10.99,
        weekend_high=15.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.37,
        count=158,
        recommended=138,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="White",
        holes=9,
        par=27,
        yardage=989,
        rating=25.0,
        slope=72,
    ),
    facility_id=16836,
    seo_name="16836-los-feliz-municipal-golf-course",
    latitude=34.12332,
    longitude=-118.269356,
)

ALONDRA_PARK = GolfNowCourse(
    key="alondra-park",
    name="Alondra Park Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "Alondra Park's regulation 18 in Lawndale, a flat, friendly par-72 of 6,445 "
        "yards from the Blue tees, run by LA County Parks. The park also has an "
        "18-hole par-3."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=31.99,
        weekday_high=62.50,
        weekend_low=36.00,
        weekend_high=69.50,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.56,
        count=494,
        recommended=380,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue",
        holes=18,
        par=72,
        yardage=6445,
        rating=70.0,
        slope=118,
    ),
    facility_id=15142,
    seo_name="15142-alondra-park-golf-course",
    latitude=33.881733,
    longitude=-118.34301,
)

CHESTER_WASHINGTON = GolfNowCourse(
    key="chester-washington",
    name="Chester Washington Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A par-70 of 6,286 yards in Los Angeles, with mature trees lining rolling "
        "fairways, undulating bent/poa greens and kikuyu tees and fairways."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=32.99,
        weekday_high=62.50,
        weekend_low=36.99,
        weekend_high=69.50,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.21,
        count=646,
        recommended=464,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue",
        holes=18,
        par=70,
        yardage=6286,
        rating=69.9,
        slope=119,
    ),
    facility_id=780,
    seo_name="780-chester-washington-golf-course",
    latitude=33.922676,
    longitude=-118.31409,
)

MAGGIE_HATHAWAY = GolfNowCourse(
    key="maggie-hathaway",
    name="Maggie Hathaway Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A 9-hole par-3 of about 1,000 yards in Los Angeles, with bunkers and city "
        "views, plus a driving range and putting green."
    ),
    green_fees=GreenFees(
        holes=9,
        weekday_low=4.50,
        weekday_high=8.25,
        weekend_low=4.50,
        weekend_high=9.50,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.68,
        count=96,
        recommended=90,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    facility_id=9380,
    seo_name="9380-maggie-hathaway-golf-course",
    latitude=33.95634,
    longitude=-118.309875,
)

LOS_VERDES = GolfNowCourse(
    key="los-verdes",
    name="Los Verdes Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A Billy Bell design (1964) in Rancho Palos Verdes, par 71 and 6,617 yards, "
        "with some of the best ocean views in California; the nearby ocean makes "
        "the greens hard to read. Known as a strong value."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=35.50,
        weekday_high=64.75,
        weekend_low=41.75,
        weekend_high=69.50,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.80,
        count=499,
        recommended=369,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue",
        holes=18,
        par=71,
        yardage=6617,
        rating=72.0,
        slope=124,
    ),
    facility_id=832,
    seo_name="832-los-verdes-golf-course",
    latitude=33.753017,
    longitude=-118.39973,
)

SCHOLL_CANYON = GolfNowCourse(
    key="scholl-canyon",
    name="Scholl Canyon Golf and Tennis Club",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A mid-length 18 in Glendale (par 60, 3,039 yards) with views, canyons to "
        "carry and large bentgrass greens; tennis on site."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=27.99,
        weekday_high=49.00,
        weekend_low=33.99,
        weekend_high=52.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.94,
        count=480,
        recommended=400,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue",
        holes=18,
        par=60,
        yardage=3039,
        rating=54.8,
        slope=86,
    ),
    facility_id=76,
    seo_name="76-scholl-canyon-golf-and-tennis-club",
    latitude=34.16092,
    longitude=-118.19922,
)

LINKS_AT_TERRANEA = GolfNowCourse(
    key="links-at-terranea",
    name="The Links at Terranea",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A Todd Eckenrode 9-hole par-3 (2009) at the Terranea resort in Rancho "
        "Palos Verdes, 1,239 yards with Pacific and Catalina views from every hole, "
        "and rated among the best short courses in the U.S. Walking only (push "
        "carts available), about 90 minutes a round, groups of up to five."
    ),
    green_fees=GreenFees(
        holes=9,
        weekday_low=40.99,
        weekday_high=105.00,
        weekend_low=48.00,
        weekend_high=105.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.42,
        count=439,
        recommended=415,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    facility_id=6140,
    seo_name="6140-the-links-at-terranea",
    latitude=33.766666,
    longitude=-118.36667,
)

ARROYO_SECO = GolfNowCourse(
    key="arroyo-seco",
    name="Arroyo Seco Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A municipal 18-hole par-3 (par 54, about 2,000 yards) in South Pasadena, a "
        "William H. Johnson design opened in 1955."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=15.99,
        weekday_high=32.00,
        weekend_low=19.99,
        weekend_high=32.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.13,
        count=337,
        recommended=299,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Mens",
        holes=18,
        par=54,
        yardage=1960,
        rating=50.6,
        slope=72,
    ),
    facility_id=15721,
    seo_name="15721-arroyo-seco-golf-course",
    latitude=34.113483,
    longitude=-118.172295,
)

BROOKSIDE_KOINER = GolfNowCourse(
    key="brookside-koiner",
    name="Brookside at the Rose Bowl – C.W. Koiner",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "Course No. 1 of Brookside, the 36-hole William P. Bell complex beside the "
        "Rose Bowl in Pasadena under the San Gabriel Mountains: the C.W. Koiner, a "
        "par-72 of 7,104 yards from the Gold tees."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=32.99,
        weekday_high=67.00,
        weekend_low=48.00,
        weekend_high=89.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.49,
        count=1230,
        recommended=903,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Gold",
        holes=18,
        par=72,
        yardage=7104,
        rating=74.5,
        slope=134,
    ),
    facility_id=66,
    seo_name="66-brookside-at-the-rose-bowl-cw-koiner",
    latitude=34.165882,
    longitude=-118.16643,
)

BROOKSIDE_NAY = GolfNowCourse(
    key="brookside-nay",
    name="Brookside at the Rose Bowl – E.O. Nay",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "Course No. 2 of Brookside, the 36-hole William P. Bell complex beside the "
        "Rose Bowl in Pasadena under the San Gabriel Mountains: the E.O. Nay, a "
        "par-70 of 6,025 yards and the shorter of the two."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=42.00,
        weekday_high=73.00,
        weekend_low=48.00,
        weekend_high=86.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.39,
        count=99,
        recommended=53,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue",
        holes=18,
        par=70,
        yardage=6025,
        rating=69.3,
        slope=121,
    ),
    facility_id=19058,
    seo_name="19058-brookside-at-the-rose-bowl-eo-nay",
    latitude=34.164898,
    longitude=-118.167,
)

BUENA_VISTA = GolfNowCourse(
    key="buena-vista",
    name="Buena Vista Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A Kern County par-72 near Taft, about 20 miles west of Bakersfield, "
        "designed by George Mifflin and opened in 1951: 6,710 yards over hilly "
        "ground with views of the Buena Vista Aquatic Recreation Area's lakes, "
        "farmland and mountains."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=27.99,
        weekday_high=51.50,
        weekend_low=36.99,
        weekend_high=59.50,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.81,
        count=129,
        recommended=107,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue",
        holes=18,
        par=72,
        yardage=6710,
        rating=72.9,
        slope=124,
    ),
    facility_id=8821,
    seo_name="8821-buena-vista-golf-course",
    latitude=35.239098,
    longitude=-119.32766,
)

MONTEREY_PARK = GolfNowCourse(
    key="monterey-park",
    name="Monterey Park Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A 9-hole public course (1973), par 29 and about 1,500 yards, open year "
        "round for a quick, informal round, with a pro shop and hitting range."
    ),
    # The only weekend rate listed when sampled was $21, so the weekend range is
    # narrower than the course's real spread.
    green_fees=GreenFees(
        holes=9,
        weekday_low=12.99,
        weekday_high=19.00,
        weekend_low=21.00,
        weekend_high=21.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.11,
        count=352,
        recommended=329,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    facility_id=9509,
    seo_name="9509-monterey-park-golf-course",
    latitude=34.060253,
    longitude=-118.16349,
)

TRUMP_NATIONAL_LA = GolfNowCourse(
    key="trump-national-la",
    name="Trump National Golf Club-Los Angeles",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A clifftop course in Rancho Palos Verdes with Pacific views from every "
        "hole and Catalina Island offshore: a par-71 of 7,242 yards from the Black "
        "tees, with a 45,000 sq ft clubhouse, restaurants and a pro shop."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=124.99,
        weekday_high=395.00,
        weekend_low=125.00,
        weekend_high=425.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.60,
        count=707,
        recommended=618,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Black",
        holes=18,
        par=71,
        yardage=7242,
        rating=75.0,
        slope=146,
    ),
    facility_id=15817,
    seo_name="15817-trump-national-golf-club-los-angeles",
    latitude=33.729614,
    longitude=-118.34955,
)

VICTORIA = GolfNowCourse(
    key="victoria",
    name="Victoria Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A municipal par-72 in Carson designed by William F. Bell and opened in "
        "1966, 6,804 yards from the Blue tees."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=18.99,
        weekday_high=45.50,
        weekend_low=18.99,
        weekend_high=48.50,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=2.05,
        count=990,
        recommended=230,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue",
        holes=18,
        par=72,
        yardage=6804,
        rating=71.8,
        slope=123,
    ),
    facility_id=418,
    seo_name="418-victoria-golf-course",
    latitude=33.853382,
    longitude=-118.2698,
)

HARBOR_PARK = GolfNowCourse(
    key="harbor-park",
    name="Harbor Park Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A regulation 9 (par 36, 3,148 yards) in Wilmington on gently rolling "
        "ground beside Lake Machado, with narrow fairways and fast, well-bunkered "
        "greens; one of the busiest nine-hole courses in North America."
    ),
    green_fees=GreenFees(
        holes=9,
        weekday_low=15.00,
        weekday_high=40.00,
        weekend_low=19.00,
        weekend_high=39.99,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.22,
        count=513,
        recommended=483,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue",
        holes=9,
        par=36,
        yardage=3148,
        rating=34.7,
        slope=117,
    ),
    facility_id=12218,
    seo_name="12218-harbor-park-golf-course",
    latitude=33.782455,
    longitude=-118.28132,
)

RANCHO_VISTA = GolfNowCourse(
    key="rancho-vista",
    name="Rancho Vista Golf Club",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A Ted Robinson par-72 in Palmdale, 6,659 yards from the Blue tees: a lush "
        "course set against the Antelope Valley desert, with a clubhouse "
        "restaurant."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=40.00,
        weekday_high=90.00,
        weekend_low=55.00,
        weekend_high=90.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.35,
        count=174,
        recommended=163,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue",
        holes=18,
        par=72,
        yardage=6659,
        rating=71.7,
        slope=127,
    ),
    facility_id=8559,
    seo_name="8559-rancho-vista-golf-club",
    latitude=34.601364,
    longitude=-118.18951,
)

ALHAMBRA = GolfNowCourse(
    key="alhambra",
    name="Alhambra Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A William F. Bell parkland par-71 (1956), only 5,466 yards from the back "
        "tees: reachable par-5s and short par-4s give birdie chances, while trees, "
        "a couple of lakes and greenside bunkers keep it honest. Four sets of tees "
        "suit seniors, juniors and shorter hitters."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=28.15,
        weekday_high=50.35,
        weekend_low=29.30,
        weekend_high=59.70,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.42,
        count=1759,
        recommended=1610,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Black",
        holes=18,
        par=71,
        yardage=5466,
        rating=65.6,
        slope=115,
    ),
    facility_id=10164,
    seo_name="10164-alhambra-golf-course",
    latitude=34.087986,
    longitude=-118.11431,
)

ANTELOPE_VALLEY_CC = GolfNowCourse(
    key="antelope-valley-cc",
    name="Antelope Valley Country Club",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A Bill Bell Sr. design (1954) in Palmdale with small, fast greens and a "
        "mix of short and long holes, 6,794 yards from the Blue tees. One of the "
        "region's best practice facilities: an all-grass range and a short-game "
        "area with a bunker."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=49.00,
        weekday_high=78.00,
        weekend_low=58.00,
        weekend_high=88.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.00,
        count=75,
        recommended=66,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue",
        holes=18,
        par=72,
        yardage=6794,
        rating=72.9,
        slope=130,
    ),
    facility_id=18006,
    seo_name="18006-antelope-valley-country-club",
    latitude=34.6056,
    longitude=-118.14165,
)

HEARTWELL = GolfNowCourse(
    key="heartwell",
    name="Heartwell Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "An 18-hole par-3 in Long Beach (par 54, 2,143 yards) near El Dorado, "
        "Skylinks and Recreation Park, with bermuda fairways and tees and poa "
        "greens."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=14.99,
        weekday_high=23.00,
        weekend_low=17.99,
        weekend_high=26.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.77,
        count=419,
        recommended=361,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    facility_id=1202,
    seo_name="1202-heartwell-golf-course",
    latitude=33.832325,
    longitude=-118.21392,
)

LOS_AMIGOS = GolfNowCourse(
    key="los-amigos",
    name="Los Amigos Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A Billy Bell par-71 (1966) in Downey, 6,212 yards, minutes from downtown "
        "Los Angeles and Orange County, with a lit driving range, a diner and a "
        "lounge."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=31.75,
        weekday_high=60.00,
        weekend_low=36.00,
        weekend_high=69.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.15,
        count=994,
        recommended=676,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Back",
        holes=18,
        par=71,
        yardage=6212,
        rating=68.7,
        slope=118,
    ),
    facility_id=8819,
    seo_name="8819-los-amigos-golf-course",
    latitude=33.93214,
    longitude=-118.16025,
)

RIO_HONDO = GolfNowCourse(
    key="rio-hondo",
    name="Rio Hondo Golf Club",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A par-71 in Downey, 6,370 yards from the Blue tees, with well-groomed "
        "fairways and greens; challenging but friendly at every level."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=33.00,
        weekday_high=76.00,
        weekend_low=37.00,
        weekend_high=85.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.61,
        count=1233,
        recommended=1027,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue",
        holes=18,
        par=71,
        yardage=6370,
        rating=70.9,
        slope=125,
    ),
    facility_id=8072,
    seo_name="8072-rio-hondo-golf-club",
    latitude=33.95353,
    longitude=-118.14803,
)

MONTEBELLO_9 = GolfNowCourse(
    key="montebello-9",
    name="The Montebello 9 at Bella Verde",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "The 9-hole course at Bella Verde in Montebello, formerly Montebello Golf "
        "Course: par 35 and 3,200 yards from the Black tees."
    ),
    green_fees=GreenFees(
        holes=9,
        weekday_low=24.99,
        weekday_high=40.78,
        weekend_low=30.99,
        weekend_high=44.66,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.49,
        count=817,
        recommended=622,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Black",
        holes=9,
        par=35,
        yardage=3200,
        rating=35.3,
        slope=120,
    ),
    facility_id=16528,
    seo_name="16528-the-montebello-9-at-bella-verde",
    latitude=34.029438,
    longitude=-118.13375,
)

BELLA_VERDE_SIGNATURE_PAR_3 = GolfNowCourse(
    key="bella-verde-signature-par-3",
    name="The Signature Par 3 at Bella Verde",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A 6-hole, walking-only par-3 at Bella Verde in Montebello with holes of 55 "
        "to 95 yards: casual golf for any level. GolfNow sells it in 6- and 9-hole "
        "rounds."
    ),
    # Every rate listed when sampled was the same, so each range is a single price.
    green_fees=GreenFees(
        holes=9,
        weekday_low=17.48,
        weekday_high=17.48,
        weekend_low=22.33,
        weekend_high=22.33,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.95,
        count=16,
        recommended=13,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    facility_id=18957,
    seo_name="18957-the-signature-par-3-at-bella-verde",
    latitude=34.028435,
    longitude=-118.130936,
)

EATON_CANYON = GolfNowCourse(
    key="eaton-canyon",
    name="Eaton Canyon Golf Club",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A regulation 9 (par 35, about 2,860 yards) at the foot of the San Gabriel "
        "Mountains in Pasadena, run by LA County since 1962; rewards precision over "
        "length. Grass-tee driving range, restaurant and bar."
    ),
    green_fees=GreenFees(
        holes=9,
        weekday_low=11.25,
        weekday_high=40.00,
        weekend_low=14.00,
        weekend_high=27.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.95,
        count=229,
        recommended=181,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue (18-hole)",
        holes=18,
        par=70,
        yardage=5724,
        rating=67.8,
        slope=122,
    ),
    facility_id=4708,
    seo_name="4708-eaton-canyon-golf-club",
    latitude=34.16532,
    longitude=-118.07939,
)

WHITTIER_NARROWS_MOUNTAIN = GolfNowCourse(
    key="whittier-narrows-mountain",
    name="Whittier Narrows 9-hole Mountain Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "The Mountain nine of Whittier Narrows' 27-hole William F. Bell layout "
        "(1955) in Rosemead, booked on its own: par 35, 2,728 yards."
    ),
    green_fees=GreenFees(
        holes=9,
        weekday_low=13.00,
        weekday_high=35.50,
        weekend_low=17.00,
        weekend_high=38.75,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.40,
        count=10,
        recommended=7,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    facility_id=19492,
    seo_name="19492-whittier-narrows-9-hole-mountain-course",
    latitude=34.05418,
    longitude=-118.07654,
)

WHITTIER_NARROWS = GolfNowCourse(
    key="whittier-narrows",
    name="Whittier Narrows Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A William F. Bell 27-hole parkland course (1955) in Rosemead with wide "
        "fairways and well-placed bunkers, played as 18-hole combinations of par "
        "71-72 up to 6,837 yards (River/Pine). Grass driving range and short-game "
        "area."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=33.99,
        weekday_high=62.25,
        weekend_low=39.25,
        weekend_high=68.75,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.24,
        count=499,
        recommended=318,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue (River/Pine)",
        holes=18,
        par=72,
        yardage=6837,
        rating=71.6,
        slope=116,
    ),
    facility_id=419,
    seo_name="419-whittier-narrows-golf-course",
    latitude=34.05418,
    longitude=-118.07654,
)

LAKEWOOD = GolfNowCourse(
    key="lakewood",
    name="Lakewood Country Club",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "An LA County championship course in Lakewood, par 72 and 7,033 yards from "
        "the Black tees, one of the longer courses around: lakes and trees "
        "throughout, tough par-3s and fairway bunkers, bermuda fairways and poa "
        "greens."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=28.99,
        weekday_high=62.50,
        weekend_low=39.50,
        weekend_high=69.50,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.62,
        count=586,
        recommended=483,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Black",
        holes=18,
        par=72,
        yardage=7033,
        rating=73.0,
        slope=128,
    ),
    facility_id=2104,
    seo_name="2104-lakewood-country-club",
    latitude=33.83413,
    longitude=-118.15383,
)

SANTA_ANITA = GolfNowCourse(
    key="santa-anita",
    name="Santa Anita Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A municipal par-71 in Arcadia designed by James Harrison Smith and opened "
        "in 1935, 6,412 yards from the Blue tees."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=32.00,
        weekday_high=60.50,
        weekend_low=36.25,
        weekend_high=69.50,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.62,
        count=881,
        recommended=667,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue",
        holes=18,
        par=71,
        yardage=6412,
        rating=70.2,
        slope=120,
    ),
    facility_id=13033,
    seo_name="13033-santa-anita-golf-course",
    latitude=34.1378,
    longitude=-118.0247,
)

ARCADIA_PAR_3 = GolfNowCourse(
    key="arcadia-par-3",
    name="Arcadia Par 3 Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "An 18-hole par-3 of 1,947 yards at the foot of the San Gabriel Mountains "
        "in Arcadia, lit for night play, with the Golf Learning Center on site."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=13.00,
        weekday_high=40.00,
        weekend_low=13.00,
        weekend_high=26.00,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.66,
        count=276,
        recommended=245,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    facility_id=1177,
    seo_name="1177-arcadia-par-3-golf-course",
    latitude=34.10476,
    longitude=-118.01635,
)

SKYLINKS = GolfNowCourse(
    key="skylinks",
    name="Skylinks Golf Course",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "A par-72 in Long Beach across Lakewood Blvd from Long Beach Airport, 6,909 "
        "yards from the Black tees, with water hazards and plenty of bunkers; lit "
        "driving range, restaurant and bar."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=28.99,
        weekday_high=78.25,
        weekend_low=37.50,
        weekend_high=100.75,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=3.96,
        count=1054,
        recommended=860,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Black",
        holes=18,
        par=72,
        yardage=6909,
        rating=72.7,
        slope=127,
    ),
    facility_id=938,
    seo_name="938-skylinks-golf-course",
    latitude=33.818096,
    longitude=-118.13808,
)

RECREATION_PARK = GolfNowCourse(
    key="recreation-park",
    name="Recreation Park Golf Course 18",
    platform="golfnow",
    timezone=PACIFIC,
    release=None,
    booking_window_days=None,
    description=(
        "Recreation Park's 18 in Long Beach, a classic Billy Bell layout and one of "
        "the oldest courses in Southern California: par 72 and 6,405 yards over "
        "rolling, tree-lined ground, with a historic-landmark clubhouse."
    ),
    green_fees=GreenFees(
        holes=18,
        weekday_low=26.99,
        weekday_high=74.50,
        weekend_low=27.99,
        weekend_high=89.75,
        sampled_on=WIDER_SAMPLED_ON,
    ),
    reviews=Reviews(
        average=4.00,
        count=755,
        recommended=640,
        sampled_on=REVIEWS_SAMPLED_ON,
    ),
    tee_rating=TeeRating(
        tee="Blue",
        holes=18,
        par=72,
        yardage=6405,
        rating=70.0,
        slope=117,
    ),
    facility_id=937,
    seo_name="937-recreation-park-golf-course-18",
    latitude=33.78035,
    longitude=-118.1355,
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
    KNOLLWOOD,
    BALBOA,
    ENCINO,
    VAN_NUYS_PAR_3,
    VAN_NUYS_EXECUTIVE,
    WOODLEY_LAKES,
    EL_CARISO,
    HEROES,
    PENMAR,
    HANSEN_DAM,
    SAND_CANYON,
    RANCHO_PARK,
    RANCHO_PARK_PAR_3,
    ALISAL_RIVER,
    ANGELES_NATIONAL,
    WESTCHESTER,
    DEBELL,
    DEBELL_PAR_3,
    LAKES_AT_EL_SEGUNDO,
    ROOSEVELT,
    WESTDRIFT_MANHATTAN_BEACH,
    HARDING,
    WILSON,
    ALONDRA_PARK_PAR_3,
    LOS_FELIZ,
    ALONDRA_PARK,
    CHESTER_WASHINGTON,
    MAGGIE_HATHAWAY,
    LOS_VERDES,
    SCHOLL_CANYON,
    LINKS_AT_TERRANEA,
    ARROYO_SECO,
    BROOKSIDE_KOINER,
    BROOKSIDE_NAY,
    BUENA_VISTA,
    MONTEREY_PARK,
    TRUMP_NATIONAL_LA,
    VICTORIA,
    HARBOR_PARK,
    RANCHO_VISTA,
    ALHAMBRA,
    ANTELOPE_VALLEY_CC,
    HEARTWELL,
    LOS_AMIGOS,
    RIO_HONDO,
    MONTEBELLO_9,
    BELLA_VERDE_SIGNATURE_PAR_3,
    EATON_CANYON,
    WHITTIER_NARROWS_MOUNTAIN,
    WHITTIER_NARROWS,
    LAKEWOOD,
    SANTA_ANITA,
    ARCADIA_PAR_3,
    SKYLINKS,
    RECREATION_PARK,
)
