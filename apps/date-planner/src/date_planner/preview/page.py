"""The preview page: the day at a glance, made to be read on a phone.

One HTML file, published as an Artifact next to the map's images: a header
with the day, its hours and sunset, the map with numbered pins and the route
between them, then the stops in order with their times, the drive or walk
between them, where sunset falls, and any booking still to make.

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

from date_planner.db.models import (
    DateStatus,
    StopBookingStatus,
    TravelMode,
    VenueType,
)
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
WALK = (
    '<svg viewBox="0 0 16 16" aria-hidden="true">'
    '<circle cx="9" cy="2.75" r="1.25"/>'
    '<path d="M8.5 5.5 7.5 10l2 1.75.75 3M7.5 10 6 14.5'
    'M8.5 5.5l2 2.25 1.75.5M8.5 5.5 6.25 7 5.75 9"/></svg>'
)
SUN = (
    '<svg viewBox="0 0 20 12" aria-hidden="true">'
    '<path d="M2 10.5h16M5.5 10.5a4.5 4.5 0 0 1 9 0M10 1.5v2'
    'M3.6 4.1l1.4 1.4M16.4 4.1 15 5.5"/></svg>'
)
PIN = (
    '<svg viewBox="0 0 16 16" aria-hidden="true">'
    '<path d="M8 14.5s4.5-4.1 4.5-7.5a4.5 4.5 0 0 0-9 0c0 3.4 4.5 7.5 4.5 7.5z"/>'
    '<circle cx="8" cy="7" r="1.5"/></svg>'
)


def _glyph(path: str) -> str:
    return (
        f'<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><path d="{path}"/></svg>'
    )


@dataclass(frozen=True)
class TypeStyle:
    label: str | None  # None: nothing worth saying, as for "other"
    color: str  # the pin's fill and the card's accent, the same in both themes
    icon: str


TYPE_STYLES = {
    VenueType.RESTAURANT: TypeStyle(
        "Restaurant",
        "#c8553d",
        _glyph("M5 2v5a1.5 1.5 0 0 0 3 0V2M6.5 2v12M12 2c-1.5 1-2 3-2 5h2v7"),
    ),
    VenueType.BAR: TypeStyle("Bar", "#8a5bc8", _glyph("M2.5 3h11L8 9zM8 9v5M5 14h6")),
    VenueType.MUSEUM: TypeStyle(
        "Museum",
        "#3f7fc1",
        _glyph("M2 6.5 8 3l6 3.5M3.5 7v5M6.5 7v5M9.5 7v5M12.5 7v5M2 13.5h12"),
    ),
    VenueType.TRAIL: TypeStyle(
        "Trail", "#3a9560", _glyph("M1.5 13.5 6 5l3 5 1.5-2.5 4 6z")
    ),
    VenueType.PARK: TypeStyle(
        "Park",
        "#5d9a2e",
        _glyph("M8 14V9M8 2.5a3.5 3.5 0 0 0-2.5 6h5A3.5 3.5 0 0 0 8 2.5z"),
    ),
    VenueType.VIEWPOINT: TypeStyle(
        "Viewpoint",
        "#1f94a3",
        _glyph("M2 12h12M4.5 12a3.5 3.5 0 0 1 7 0M8 3v2.5M3.5 5.5 5 7M12.5 5.5 11 7"),
    ),
    VenueType.OTHER: TypeStyle(
        None,
        "#6b7885",
        _glyph("M8 14.5s4.5-4.1 4.5-7.5a4.5 4.5 0 0 0-9 0c0 3.4 4.5 7.5 4.5 7.5z"),
    ),
}


def type_style(venue_type: VenueType | None) -> TypeStyle:
    return TYPE_STYLES[venue_type or VenueType.OTHER]


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
    # How the leg into this stop is travelled.
    travel_mode: TravelMode
    booking_status: StopBookingStatus
    booking_url: str | None
    confirmation_number: str | None
    on_calendar: bool
    # Where the pin goes, as fractions of the map; None when off the map.
    place: tuple[float, float] | None
    venue_type: VenueType | None = None
    website: str | None = None
    duration_min: int | None = None


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


def duration(minutes: int) -> str:
    """90 -> "1 hr 30 min"."""
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{rest} min"
    return f"{hours} hr" + (f" {rest} min" if rest else "")


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


def _state(stop: PreviewStop) -> str:
    """How the stop's booking looks on its pin: done, still to do, or off."""
    status = stop.booking_status
    if status is StopBookingStatus.CONFIRMED:
        return "ok"
    if status in (
        StopBookingStatus.NEEDED,
        StopBookingStatus.LINK_SENT,
        StopBookingStatus.REMINDER_SET,
    ):
        return "todo"
    return "off" if status is StopBookingStatus.CANCELLED else ""


STATE_MARK = {"ok": "✓", "todo": "!"}


def _disc(stop: PreviewStop) -> str:
    """The pin: the venue's type as a glyph, its number in one corner and
    its booking state in the other."""
    style = type_style(stop.venue_type)
    state = _state(stop)
    mark = STATE_MARK.get(state)
    badge = f'<span class="st">{mark}</span>' if mark else ""
    return (
        f'<span class="disc" data-state="{state}" style="--t:{style.color}">'
        f'{style.icon}<span class="n">{stop.number}</span>{badge}</span>'
    )


def _length(stop: PreviewStop) -> str:
    return duration(stop.duration_min) if stop.duration_min else ""


def _kind(stop: PreviewStop) -> str:
    """"Restaurant · 1 hr 30 min", either part left out when unknown."""
    style = type_style(stop.venue_type)
    bits = [b for b in (style.label, _length(stop)) if b]
    if not bits:
        return ""
    icon = style.icon if style.label else ""
    return f'<div class="kind">{icon}{_e(" · ".join(bits))}</div>'


def _popup(stop: PreviewStop) -> str:
    """What opens when the stop's pin is tapped. Every control is a button
    or link sized for a thumb: there's no hover on a phone."""
    style = type_style(stop.venue_type)
    when = " · ".join(b for b in (_hours(stop), _length(stop)) if b)
    place = apple_place(stop)
    actions = f'<button type="button" data-goto="{stop.number}">Details</button>'
    if place:
        actions += (
            f'<a href="{_e(place)}" target="_blank" rel="noopener">Apple Maps</a>'
        )
    if stop.website:
        actions += (
            f'<a href="{_e(stop.website)}" target="_blank" rel="noopener">Website</a>'
        )
    booking = _booking(stop)
    return (
        '<div class="pop">'
        + (f'<div class="kind">{style.icon}{_e(style.label)}</div>' if style.label else "")
        + f"<strong>{stop.number}. {_e(stop.title)}</strong>"
        + (f"<span>{_e(when)}</span>" if when else "")
        + (f'<span class="addr">{_e(stop.address)}</span>' if stop.address else "")
        + (f'<div class="row">{booking}</div>' if booking else "")
        + f'<div class="acts">{actions}</div></div>'
    )


def _stop(stop: PreviewStop, live: bool = False) -> str:
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
    # Hidden until the interactive map has loaded; the script reveals it.
    locate = (
        f'<button type="button" class="locate" data-locate="{stop.number}" hidden'
        f' aria-label="Show {_e(stop.title)} on the map">{PIN}</button>'
        if live and stop.place is not None and stop.lat is not None
        else ""
    )
    return f"""
<li class="stop{cancelled}" id="stop-{stop.number}" style="--t:{type_style(stop.venue_type).color}">
  <div class="when">{when}</div>
  <div class="what">
    <div class="head">
      <span class="num" aria-hidden="true">{stop.number}</span>
      <h2>{_e(stop.title)}</h2>
      {locate}
    </div>
    {_kind(stop)}
    {row}
  </div>
</li>"""


def _leg(minutes: int, mode: TravelMode) -> str:
    if mode is TravelMode.WALKING:
        return f'<li class="leg">{WALK}{minutes} min walk</li>'
    return f'<li class="leg">{CAR}{minutes} min drive</li>'


def _sunset(at: datetime) -> str:
    return f'<li class="sun">{SUN}Sunset {clock(at)}</li>'


def _timeline(data: PreviewData) -> str:
    items: list[str] = []
    sunset_placed = data.sunset is None
    for i, stop in enumerate(data.stops):
        if i and stop.travel_min_from_prev:
            items.append(_leg(stop.travel_min_from_prev, stop.travel_mode))
        if (
            not sunset_placed
            and data.sunset is not None
            and stop.start_at is not None
            and stop.start_at >= data.sunset
        ):
            items.append(_sunset(data.sunset))
            sunset_placed = True
        items.append(_stop(stop, live=data.tiles is not None))
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
        f'top:{s.place[1] * 100:.2f}%" title="{_e(s.title)}">{_disc(s)}</span>'
        for s in placed
        if s.place
    )
    images = ""
    if data.has_light_map:
        # No dark tiles: the page darkens the light ones (see page.css).
        dimmed = "" if data.has_dark_map else " dimmed"
        images += f'<img class="tiles light{dimmed}" src="{LIGHT_MAP}" alt="">'
    if data.has_dark_map:
        images += f'<img class="tiles dark" src="{DARK_MAP}" alt="">'
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
    const motion = matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
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
    // Walking legs dotted, driving legs solid, each labelled where it ends
    // up halfway: nothing here needs a hover.
    cfg.stops.forEach((s, i) => {
      if (!i) return;
      const p = cfg.stops[i - 1];
      L.polyline([[p.lat, p.lng], [s.lat, s.lng]], {
        className: "live-route " + (s.walking ? "walk" : "drive"), interactive: false,
      }).addTo(map);
      if (!s.leg) return;
      L.marker([(p.lat + s.lat) / 2, (p.lng + s.lng) / 2], {
        icon: L.divIcon({ className: "leg-tag", html: "<span>" + s.leg + "</span>", iconSize: [0, 0] }),
        interactive: false, keyboard: false, zIndexOffset: -500,
      }).addTo(map);
    });
    const markers = new Map();
    for (const s of cfg.stops) {
      const icon = L.divIcon({ className: "live-pin", html: s.pin, iconSize: [34, 34] });
      const marker = L.marker([s.lat, s.lng], { icon, title: s.title, keyboard: true })
        .bindPopup(s.popup, {
          closeButton: false, offset: [0, -12], minWidth: 210, maxWidth: 270,
          autoPanPadding: [16, 16],
        })
        .addTo(map);
      // The time sits under the pin all the time, so it's there to read.
      if (s.at) {
        marker.bindTooltip(s.at, {
          permanent: true, direction: "bottom", offset: [0, 14], className: "live-tip",
        });
      }
      markers.set(s.number, marker);
    }
    // Popup -> its card. The popup stops clicks reaching the map, so the
    // button gets its own listener each time one opens.
    map.on("popupopen", (e) => {
      const button = e.popup.getElement().querySelector("[data-goto]");
      if (!button) return;
      button.addEventListener("click", () => {
        const card = document.getElementById("stop-" + button.dataset.goto);
        if (!card) return;
        card.scrollIntoView({ behavior: motion, block: "center" });
        card.classList.remove("flash");
        void card.offsetWidth;
        card.classList.add("flash");
      });
    });
    const frame = live.closest(".frame");
    // Card -> its pin.
    for (const button of document.querySelectorAll("[data-locate]")) {
      button.addEventListener("click", () => {
        const marker = markers.get(Number(button.dataset.locate));
        if (!marker) return;
        frame.scrollIntoView({ behavior: motion, block: "start" });
        map.panTo(marker.getLatLng());
        marker.openPopup();
      });
    }
    const points = cfg.stops.map((s) => [s.lat, s.lng]);
    map.fitBounds(points, { padding: [44, 44], maxZoom: cfg.zoom });
    base.once("load", () => {
      frame.classList.add("is-live");
      for (const button of document.querySelectorAll("[data-locate]")) button.hidden = false;
      // No longer a picture: let the pins and the zoom buttons through.
      frame.removeAttribute("role");
      frame.removeAttribute("aria-label");
    });
  } catch {
    live.remove();
  }
})();
"""


def _leg_tag(stop: PreviewStop) -> str:
    """The short label on the leg into `stop`: its icon and minutes."""
    if not stop.travel_min_from_prev:
        return ""
    icon = WALK if stop.travel_mode is TravelMode.WALKING else CAR
    return f"{icon}{stop.travel_min_from_prev} min"


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
                # Under the pin, where it can be read without tapping.
                "at": clock(s.start_at) if s.start_at else "",
                "pin": _disc(s),
                "popup": _popup(s),
                "walking": s.travel_mode is TravelMode.WALKING,
                "leg": _leg_tag(s) if i else "",
            }
            for i, s in enumerate(stops)
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
    Times in {_e(data.tz_label)}.
    Legs between stops are kept under {data.max_leg_minutes} min.
  </footer>
</main>
"""
