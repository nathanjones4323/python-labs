"""The preview page: the day at a glance, made to be read on a phone.

One HTML file, published as an Artifact next to the map's images: a header
with the day, its hours and sunset, the map with numbered pins and the route
between them, then the stops in order with their times, the drive between
them, where sunset falls, and any booking still to make.

The Artifact host wraps the file in its own document skeleton, so there's
no <html>/<head>/<body> here. Its CSP allows Google Fonts, scripts from a
few CDNs, and nothing else from outside: Leaflet comes from cdnjs at a
pinned version, its stylesheet is inlined, and its tiles ship next to the
page (see `basemap`). The map is drawn without it first, as a picture with
the pins on top, and Leaflet takes over once it has loaded.
"""

import json
from dataclasses import dataclass
from datetime import date, datetime
from html import escape
from pathlib import Path
from urllib.parse import quote, urlencode

from date_planner.db.models import DateStatus, StopBookingStatus, TravelMode
from date_planner.preview.basemap import TILE_FORMAT, Pyramid

LIGHT_MAP = "map-light.jpg"
DARK_MAP = "map-dark.jpg"
TILE_URL = "tiles/{z}/{x}/{y}." + TILE_FORMAT
_HERE = Path(__file__).parent
CSS = (_HERE / "page.css").read_text(encoding="utf-8")
# leaflet.css 1.9.4 as cdnjs serves it, inlined: the CSP takes stylesheets
# from Google Fonts only.
LEAFLET_CSS = (_HERE / "leaflet.css").read_text(encoding="utf-8")
LEAFLET_JS = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"
LEAFLET_SRI = (
    "sha512-BwHfrr4c9kmRkLw6iXFdzcdWV/PGkVgiIyIWLLlTSXzWQzxuSg4DiQUCpauz"
    "/EWjgk5TYQqX/kvn9pG1NpYfqg=="
)
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
    address: str | None
    phone: str | None
    lat: float | None
    lng: float | None
    start_at: datetime | None  # already in the couple's timezone
    end_at: datetime | None
    travel_min_from_prev: int | None
    travel_mode: TravelMode  # how this stop is reached from the one before
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
    # Tiles for the interactive map; None leaves the picture as it is.
    tiles: Pyramid | None = None
    # The zoom the picture is framed at, which the interactive map opens at
    # or above.
    map_zoom: int = 0


# The header only carries a status when there's something to do about it.
STATUS_CHIP = {
    DateStatus.BOOKING_PENDING: '<span class="chip todo">Bookings pending</span>',
    DateStatus.CANCELLED: '<span class="chip off">Cancelled</span>',
}


def clock(at: datetime) -> str:
    """6:10 PM — no leading zero, the way people write times."""
    return at.strftime("%I:%M %p").lstrip("0")


def _e(value: object) -> str:
    return escape(str(value), quote=True)


# Apple Maps' unified URLs (iOS 18.4+, macOS 15.4+): https links that open
# the Maps app on an iPhone and maps.apple.com anywhere else.
# https://developer.apple.com/documentation/mapkit/unified-map-urls
APPLE_MAPS = "https://maps.apple.com"


def _query(params: list[tuple[str, str]]) -> str:
    # Spaces as %20 and commas left alone, as Apple's own examples have them.
    return urlencode(params, quote_via=quote, safe=",")


def apple_place(stop: PreviewStop) -> str | None:
    """The stop's place card: a pin at its coordinates under its name, or
    its address when it has no coordinates."""
    name = stop.venue_name or stop.title
    if stop.lat is not None and stop.lng is not None:
        params = [("coordinate", f"{stop.lat},{stop.lng}"), ("name", name)]
    elif stop.address:
        params = [("address", stop.address)]
    else:
        return None
    return f"{APPLE_MAPS}/place?{_query(params)}"


def apple_route(stops: list[PreviewStop]) -> str | None:
    """Directions through every stop with coordinates, in order: the first
    is the start, the last the destination, the rest repeated `waypoint`s.
    One mode covers the whole route, so it's walking only when every leg
    is."""
    located = [s for s in stops if s.lat is not None and s.lng is not None]
    if len(located) < 2:
        return None
    points = [f"{s.lat},{s.lng}" for s in located]
    walking = all(s.travel_mode is TravelMode.WALKING for s in located[1:])
    params = [("source", points[0]), ("destination", points[-1])]
    params += [("waypoint", point) for point in points[1:-1]]
    params.append(("mode", "walking" if walking else "driving"))
    return f"{APPLE_MAPS}/directions?{_query(params)}"


def google_calendar_day(day: date) -> str:
    """The day in Google Calendar. It's the web calendar: the iPhone app
    claims no https links, and a custom scheme can't be relied on from
    inside the Artifact's sandboxed frame."""
    return (
        f"https://calendar.google.com/calendar/r/day/{day.year}/{day.month}/{day.day}"
    )


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
    return ""


def _stop(stop: PreviewStop) -> str:
    if stop.start_at is not None:
        end = f"<span>{clock(stop.end_at)}</span>" if stop.end_at else ""
        when = f"<time>{clock(stop.start_at)}</time>{end}"
    else:
        when = "<time>Time TBD</time>"
    place = apple_place(stop)
    row = _booking(stop)
    if place:
        row += f'<a href="{_e(place)}" target="_blank" rel="noopener">Apple Maps</a>'
    row = f'<div class="row">{row}</div>' if row else ""
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
    {row}
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
    route_link = apple_route(data.stops)
    open_route = (
        f'<a class="route-link" href="{_e(route_link)}" target="_blank"'
        ' rel="noopener">Route in Apple Maps</a>'
        if route_link
        else ""
    )
    labels = ", ".join(f"{s.number}: {s.title}" for s in placed)
    return f"""
<figure class="map">
  <div class="frame{"" if data.has_light_map else " bare"}"
       style="aspect-ratio:{w}/{h}" role="img"
       aria-label="Map of the stops. {_e(labels)}">
    {images}{line}{pins}{_live(data)}
  </div>
  <figcaption><span>{credit}</span>{open_route}</figcaption>
</figure>"""


# Builds the Leaflet map over the picture and shows it once its first tiles
# are in. Anything going wrong leaves the picture where it is.
LIVE_JS = """
(() => {
  const live = document.getElementById("live-map");
  const data = document.getElementById("live-map-data");
  if (!live || !data || !window.L) return;
  try {
    const cfg = JSON.parse(data.textContent);
    const box = (b) => [[b[0], b[1]], [b[2], b[3]]];
    const map = L.map(live, {
      minZoom: cfg.minZoom, maxZoom: cfg.maxZoom,
      maxBounds: box(cfg.bounds), maxBoundsViscosity: 1,
      attributionControl: false, scrollWheelZoom: false,
    });
    const base = L.tileLayer(cfg.url, {
      minZoom: cfg.minZoom, maxZoom: cfg.maxZoom, maxNativeZoom: cfg.fullZoom,
    }).addTo(map);
    for (const p of cfg.patches) {
      L.tileLayer(cfg.url, {
        minZoom: p.zoom, maxZoom: p.zoom, bounds: box(p.bounds), zIndex: 2,
      }).addTo(map);
    }
    const points = cfg.stops.map((s) => [s.lat, s.lng]);
    if (points.length > 1) {
      L.polyline(points, { className: "live-route", interactive: false }).addTo(map);
    }
    for (const s of cfg.stops) {
      const pop = document.createElement("div");
      pop.className = "pop";
      const name = document.createElement("strong");
      name.textContent = s.number + ". " + s.title;
      pop.append(name);
      if (s.when) {
        const when = document.createElement("span");
        when.textContent = s.when;
        pop.append(when);
      }
      if (s.maps) {
        const a = document.createElement("a");
        a.href = s.maps;
        a.target = "_blank";
        a.rel = "noopener";
        a.textContent = "Apple Maps";
        pop.append(a);
      }
      const icon = L.divIcon({
        className: "live-pin", html: String(s.number), iconSize: [26, 26],
      });
      L.marker([s.lat, s.lng], { icon, title: s.title, keyboard: true })
        .bindPopup(pop, { closeButton: false, offset: [0, -8] })
        .addTo(map);
    }
    map.fitBounds(points, { padding: [36, 36], maxZoom: cfg.zoom });
    base.once("load", () => {
      const frame = live.closest(".frame");
      frame.classList.add("is-live");
      // No longer a picture: let the pins and the zoom buttons through.
      frame.removeAttribute("role");
      frame.removeAttribute("aria-label");
    });
  } catch {
    live.remove();
  }
})();
"""


def _live(data: PreviewData) -> str:
    """The interactive map's container, settings and script."""
    stops = [
        s
        for s in data.stops
        if s.place is not None and s.lat is not None and s.lng is not None
    ]
    if data.tiles is None or not stops:
        return ""
    tiles = data.tiles
    config = {
        "url": TILE_URL,
        "minZoom": tiles.min_zoom,
        "fullZoom": tiles.full_zoom,
        "maxZoom": tiles.max_zoom,
        "zoom": min(max(data.map_zoom, tiles.min_zoom), tiles.max_zoom),
        "bounds": [round(v, 6) for v in tiles.bounds],
        "patches": [
            {"zoom": p.zoom, "bounds": [round(v, 6) for v in p.bounds]}
            for p in tiles.patches
        ],
        "stops": [
            {
                "number": s.number,
                "title": s.title,
                "lat": s.lat,
                "lng": s.lng,
                "when": _hours(s),
                "maps": apple_place(s),
            }
            for s in stops
        ],
    }
    # No "<" at all inside <script>, so no title can end the block early.
    payload = json.dumps(config, ensure_ascii=False).replace("<", "\\u003c")
    return f"""
    <div class="live" id="live-map"></div>
    <script type="application/json" id="live-map-data">{payload}</script>
    <script src="{LEAFLET_JS}" integrity="{LEAFLET_SRI}"
            crossorigin="anonymous" referrerpolicy="no-referrer"></script>
    <script>{LIVE_JS}</script>"""


def _hours(stop: PreviewStop) -> str:
    if stop.start_at is None:
        return ""
    if stop.end_at is None:
        return clock(stop.start_at)
    return f"{clock(stop.start_at)}–{clock(stop.end_at)}"


def _summary(data: PreviewData) -> str:
    """Sat, Oct 17 · 9:30 AM–3:15 PM · sunset 6:16 PM"""
    bits = [data.day.strftime("%a, %b ") + str(data.day.day)]
    timed = [s for s in data.stops if s.start_at is not None]
    if timed:
        first = min(s.start_at for s in timed if s.start_at)
        last = max((s.end_at or s.start_at) for s in timed if s.start_at)
        bits.append(f"{clock(first)}–{clock(last)}")
    if data.sunset is not None:
        bits.append(f"sunset {clock(data.sunset)}")
    return " · ".join(_e(b) for b in bits)


def _calendar(data: PreviewData) -> str:
    if not any(s.on_calendar for s in data.stops):
        return ""
    return (
        f'<a class="button" href="{_e(google_calendar_day(data.day))}"'
        ' target="_blank" rel="noopener">Open in Google Calendar</a>'
    )


def render(data: PreviewData) -> str:
    missing = [s for s in data.stops if s.place is None]
    off_map = (
        '<p class="note">Not on the map yet: '
        + ", ".join(_e(s.title) for s in missing)
        + " (no venue location saved).</p>"
        if missing and len(missing) < len(data.stops)
        else ""
    )
    leaflet = f"<style>\n{LEAFLET_CSS}</style>\n" if data.tiles else ""
    return f"""<title>{_e(data.title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONTS}">
{leaflet}<style>
{CSS}</style>
<main class="wrap">
  <header>
    <h1>{_e(data.title)}</h1>
    <div class="meta">
      {STATUS_CHIP.get(data.status, "")}
      <span>{_summary(data)}</span>
    </div>
    {_calendar(data)}
  </header>
  {_map(data)}
  {off_map}
  <ol class="day">
    {_timeline(data)}
  </ol>
  <footer>
    Times in {_e(data.tz_label)}. Drives are kept under {data.max_leg_minutes} min.
  </footer>
</main>
"""
