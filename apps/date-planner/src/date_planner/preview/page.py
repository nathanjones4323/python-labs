"""The preview page: the day at a glance, made to be read on a phone.

One self-contained HTML file, published as an Artifact next to the basemap
images: a header with the day, status and sunset, the map with numbered pins
and the route between them, then the stops in order with their times, the
drive between them, where sunset falls, and how each one's booking stands.

The Artifact host wraps the file in its own document skeleton, so there's
no <html>/<head>/<body> here, and its CSP allows Google Fonts and nothing
else from outside: no scripts at all are needed.
"""

from dataclasses import dataclass
from datetime import date, datetime
from html import escape
from pathlib import Path
from urllib.parse import urlencode

from date_planner.db.models import DateStatus, StopBookingStatus, VenueType

LIGHT_MAP = "map-light.jpg"
DARK_MAP = "map-dark.jpg"
CSS = (Path(__file__).with_name("page.css")).read_text(encoding="utf-8")
FONTS = (
    "https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght"
    "@12..96,500;12..96,650&family=Figtree:wght@400;500;600&display=swap"
)

ARROW = (
    '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M4 2h6v6M10 2 3 9"/></svg>'
)
CAR = (
    '<svg viewBox="0 0 16 16" aria-hidden="true">'
    '<path d="M3 9.5 4.4 5.6A1.5 1.5 0 0 1 5.8 4.6h4.4a1.5 1.5 0 0 1 1.4 1'
    'L13 9.5v3H3z"/>'
    '<circle cx="5.5" cy="10.5" r=".6"/><circle cx="10.5" cy="10.5" r=".6"/></svg>'
)
SUN = (
    '<svg viewBox="0 0 20 12" aria-hidden="true">'
    '<path d="M2 10.5h16M5.5 10.5a4.5 4.5 0 0 1 9 0M10 1.5v2'
    'M3.6 4.1l1.4 1.4M16.4 4.1 15 5.5"/></svg>'
)


@dataclass(frozen=True)
class PreviewStop:
    number: int
    title: str
    venue_name: str | None
    venue_type: VenueType | None
    address: str | None
    phone: str | None
    lat: float | None
    lng: float | None
    start_at: datetime | None  # already in the couple's timezone
    end_at: datetime | None
    travel_min_from_prev: int | None
    booking_status: StopBookingStatus
    booking_url: str | None
    confirmation_number: str | None
    on_calendar: bool
    # Where the pin goes, as fractions of the map; None when off the map.
    place: tuple[float, float] | None


@dataclass(frozen=True)
class PreviewData:
    title: str
    day: date
    tz_label: str
    status: DateStatus
    max_leg_minutes: int
    sunset: datetime | None
    stops: list[PreviewStop]
    map_credit: str
    map_width: int
    map_height: int
    has_light_map: bool
    has_dark_map: bool


STATUS_LABEL = {
    DateStatus.PLANNING: "Planning",
    DateStatus.BOOKING_PENDING: "Bookings pending",
    DateStatus.CONFIRMED: "All booked",
    DateStatus.COMPLETED: "Done",
    DateStatus.CANCELLED: "Cancelled",
}
STATUS_TONE = {
    DateStatus.PLANNING: "quiet",
    DateStatus.BOOKING_PENDING: "todo",
    DateStatus.CONFIRMED: "ok",
    DateStatus.COMPLETED: "quiet",
    DateStatus.CANCELLED: "off",
}
VENUE_LABEL = {
    VenueType.RESTAURANT: "Restaurant",
    VenueType.TRAIL: "Trail",
    VenueType.MUSEUM: "Museum",
    VenueType.BAR: "Bar",
    VenueType.PARK: "Park",
    VenueType.VIEWPOINT: "Viewpoint",
    VenueType.OTHER: "",
}


def clock(at: datetime) -> str:
    """6:10 PM — no leading zero, the way people write times."""
    return at.strftime("%I:%M %p").lstrip("0")


def _e(value: object) -> str:
    return escape(str(value), quote=True)


def _maps_search(stop: PreviewStop) -> str | None:
    if stop.lat is not None and stop.lng is not None:
        query = f"{stop.lat},{stop.lng}"
    elif stop.address:
        query = stop.address
    else:
        return None
    return "https://www.google.com/maps/search/?" + urlencode(
        {"api": 1, "query": query}
    )


def _maps_route(stops: list[PreviewStop]) -> str | None:
    points = [
        f"{s.lat},{s.lng}" for s in stops if s.lat is not None and s.lng is not None
    ]
    if len(points) < 2:
        return None
    params = {"api": 1, "origin": points[0], "destination": points[-1]}
    if len(points) > 2:
        params["waypoints"] = "|".join(points[1:-1])
    params["travelmode"] = "driving"
    return "https://www.google.com/maps/dir/?" + urlencode(params)


def _booking(stop: PreviewStop) -> str:
    """The stop's booking state as a chip, plus the one thing to do next."""
    status = stop.booking_status
    if status is StopBookingStatus.CONFIRMED:
        ref = (
            f' <span class="ref">#{_e(stop.confirmation_number)}</span>'
            if stop.confirmation_number
            else ""
        )
        return f'<span class="chip ok">Booked{ref}</span>'
    if status is StopBookingStatus.LINK_SENT and stop.booking_url:
        return (
            f'<a class="chip todo act" href="{_e(stop.booking_url)}" '
            f'target="_blank" rel="noopener">Book now{ARROW}</a>'
        )
    if status is StopBookingStatus.LINK_SENT:
        return '<span class="chip todo">Booking link sent</span>'
    if status is StopBookingStatus.REMINDER_SET:
        call = f' <span class="ref">{_e(stop.phone)}</span>' if stop.phone else ""
        return f'<span class="chip todo">Call to book{call}</span>'
    if status is StopBookingStatus.NEEDED:
        return '<span class="chip todo">Needs booking</span>'
    if status is StopBookingStatus.CANCELLED:
        return '<span class="chip off">Cancelled</span>'
    return '<span class="chip quiet">No booking needed</span>'


def _stop(stop: PreviewStop) -> str:
    if stop.start_at is not None:
        end = f"<span>{clock(stop.end_at)}</span>" if stop.end_at else ""
        when = f"<time>{clock(stop.start_at)}</time>{end}"
    else:
        when = "<time>Time TBD</time>"
    kind = VENUE_LABEL.get(stop.venue_type, "") if stop.venue_type else ""
    where_bits = [b for b in (stop.venue_name, kind) if b]
    if stop.venue_name is None:
        where_bits = ["Venue still to choose"]
    where = " · ".join(_e(b) for b in where_bits)
    directions = _maps_search(stop)
    links = []
    if directions:
        links.append(
            f'<a href="{_e(directions)}" target="_blank" rel="noopener">Map</a>'
        )
    if stop.on_calendar:
        links.append('<span class="cal">On calendar</span>')
    address = f'<p class="addr">{_e(stop.address)}</p>' if stop.address else ""
    cancelled = (
        " cancelled" if stop.booking_status is StopBookingStatus.CANCELLED else ""
    )
    return f"""
<li class="stop{cancelled}">
  <div class="when">{when}</div>
  <div class="what">
    <div class="head">
      <span class="num" aria-hidden="true">{stop.number}</span>
      <h2>{_e(stop.title)}</h2>
    </div>
    <p class="where">{where}</p>
    {address}
    <div class="row">{_booking(stop)}{"".join(links)}</div>
  </div>
</li>"""


def _leg(minutes: int) -> str:
    return f'<li class="leg">{CAR}{minutes} min drive</li>'


def _sunset(at: datetime) -> str:
    return f'<li class="sun">{SUN}Sunset {clock(at)}</li>'


def _timeline(data: PreviewData) -> str:
    items: list[str] = []
    sunset_placed = data.sunset is None
    for i, stop in enumerate(data.stops):
        if i and stop.travel_min_from_prev:
            items.append(_leg(stop.travel_min_from_prev))
        if (
            not sunset_placed
            and data.sunset is not None
            and stop.start_at is not None
            and stop.start_at >= data.sunset
        ):
            items.append(_sunset(data.sunset))
            sunset_placed = True
        items.append(_stop(stop))
    timed = [s for s in data.stops if s.start_at is not None]
    if not sunset_placed and data.sunset is not None and timed:
        items.append(_sunset(data.sunset))
    return "\n".join(items)


def _map(data: PreviewData) -> str:
    placed = [s for s in data.stops if s.place is not None]
    if not placed:
        return ""
    w, h = data.map_width, data.map_height
    route = " ".join(
        f"{s.place[0] * w:.1f},{s.place[1] * h:.1f}" for s in placed if s.place
    )
    line = (
        f'<svg class="route" viewBox="0 0 {w} {h}" preserveAspectRatio="none"'
        f' aria-hidden="true"><polyline points="{route}"/></svg>'
        if len(placed) > 1
        else ""
    )
    pins = "".join(
        f'<span class="pin" style="left:{s.place[0] * 100:.2f}%;'
        f'top:{s.place[1] * 100:.2f}%" title="{_e(s.title)}">{s.number}</span>'
        for s in placed
        if s.place
    )
    images = ""
    if data.has_light_map:
        images += f'<img class="tiles light" src="{LIGHT_MAP}" alt="">'
    if data.has_dark_map:
        images += f'<img class="tiles dark" src="{DARK_MAP}" alt="">'
    elif data.has_light_map:
        # No dark tiles: the page darkens the light ones (see page.css).
        images = images.replace('class="tiles light"', 'class="tiles light dimmed"')
    credit = (
        _e(data.map_credit)
        if data.has_light_map
        else "Map tiles unavailable; pins are to scale"
    )
    route_link = _maps_route(data.stops)
    open_route = (
        f'<a class="route-link" href="{_e(route_link)}" target="_blank"'
        ' rel="noopener">Open the route in Google Maps</a>'
        if route_link
        else ""
    )
    labels = ", ".join(f"{s.number}: {s.title}" for s in placed)
    return f"""
<figure class="map">
  <div class="frame{"" if data.has_light_map else " bare"}"
       style="aspect-ratio:{w}/{h}" role="img"
       aria-label="Map of the stops. {_e(labels)}">
    {images}{line}{pins}
  </div>
  <figcaption><span>{credit}</span>{open_route}</figcaption>
</figure>"""


def _summary(data: PreviewData) -> str:
    timed = [s for s in data.stops if s.start_at is not None]
    bits = [f"{len(data.stops)} stop{'s' if len(data.stops) != 1 else ''}"]
    if timed:
        first = timed[0].start_at
        last = max((s.end_at or s.start_at) for s in timed if s.start_at)
        if first and last:
            bits.append(f"{clock(first)} – {clock(last)}")
    if data.sunset is not None:
        bits.append(f"sunset {clock(data.sunset)}")
    return " · ".join(_e(b) for b in bits)


def render(data: PreviewData) -> str:
    missing = [s for s in data.stops if s.place is None]
    off_map = (
        '<p class="note">Not on the map yet: '
        + ", ".join(_e(s.title) for s in missing)
        + " (no venue location saved).</p>"
        if missing and len(missing) < len(data.stops)
        else ""
    )
    day = data.day.strftime("%A, %B ") + str(data.day.day)
    return f"""<title>{_e(data.title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONTS}">
<style>
{CSS}</style>
<main class="wrap">
  <header>
    <p class="eyebrow">{_e(day)}</p>
    <h1>{_e(data.title)}</h1>
    <div class="meta">
      <span class="chip {STATUS_TONE[data.status]}">{STATUS_LABEL[data.status]}</span>
      <span>{_summary(data)}</span>
    </div>
  </header>
  {_map(data)}
  {off_map}
  <ol class="day">
    {_timeline(data)}
  </ol>
  <footer>
    Times in {_e(data.tz_label)}. Drives are kept under {data.max_leg_minutes} min.
    Nothing is booked from this page: tentative holds stay that way until you book.
  </footer>
</main>
"""
