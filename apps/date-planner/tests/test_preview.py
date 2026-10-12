"""The date preview: framing the map, stitching tiles, and the page itself."""

import io
import json
import os
import re
import uuid
from datetime import date, datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from date_planner import preview
from date_planner.booking.channels import BookingChannel
from date_planner.config import Settings, get_settings
from date_planner.db.models import (
    Anchor,
    DateStatus,
    DateStop,
    PlannedDate,
    ReservationNeed,
    StopBookingStatus,
    StopKind,
    TravelMode,
    Venue,
    VenueType,
)
from date_planner.preview import basemap, page
from date_planner.server import create_server
from fastmcp import Client
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

LA = ZoneInfo("America/Los_Angeles")
SOLSTICE = (34.0378, -118.7468)
GETTY = (34.0459, -118.5648)
NOBU = (34.0389596, -118.6696041)
TILES = "https://tiles.example.com/{z}/{x}/{y}@2x.png"
# The Griffith day: a trailhead, and two stops a block apart downtown.
GRIFFITH = (34.1195, -118.3001)
MARKET = (34.0508, -118.2489)
ANGELS = (34.0513, -118.2497)


# --- framing ---------------------------------------------------------------


def test_every_stop_lands_inside_the_padded_frame() -> None:
    frame = basemap.fit([SOLSTICE, GETTY, NOBU], 400, 260)
    for lat, lng in (SOLSTICE, GETTY, NOBU):
        x, y = frame.place(lat, lng)
        assert basemap.PAD_X / 400 - 1e-6 <= x <= 1 - basemap.PAD_X / 400 + 1e-6
        assert basemap.PAD_Y / 260 - 1e-6 <= y <= 1 - basemap.PAD_Y / 260 + 1e-6
    # One zoom closer and the stops no longer fit.
    closer = basemap.Frame(frame.zoom + 1, 0, 0, 400, 260)
    xs = [basemap.world_px(lat, lng, closer.zoom)[0] for lat, lng in (SOLSTICE, GETTY)]
    assert max(xs) - min(xs) > 400 - 2 * basemap.PAD_X


def test_one_stop_is_centred_at_neighbourhood_zoom() -> None:
    frame = basemap.fit([GETTY], 400, 260)
    assert frame.zoom == 14
    x, y = frame.place(*GETTY)
    assert x == pytest.approx(0.5, abs=0.01)
    assert y == pytest.approx(0.5, abs=0.01)


def test_the_tiles_cover_the_frame() -> None:
    frame = basemap.Frame(zoom=10, left=300, top=500, width=400, height=260)
    # 300..699 spans tiles 1-2; 500..759 spans tiles 1-2.
    assert frame.tiles() == [(1, 1), (2, 1), (1, 2), (2, 2)]


def _png(colour: str, side: int = 512) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (side, side), colour).save(out, "PNG")
    return out.getvalue()


@respx.mock
async def test_tiles_are_stitched_and_cached(tmp_path: Path) -> None:
    route = respx.get(url__regex=r"https://tiles\.example\.com/.*").mock(
        return_value=httpx.Response(200, content=_png("#336699"))
    )
    frame = basemap.fit([SOLSTICE, GETTY], 400, 260)
    cache = basemap.TileCache(tmp_path, TILES, ttl_days=30)
    async with httpx.AsyncClient() as client:
        first = await basemap.render(frame, cache, client)
        calls = route.call_count
        second = await basemap.render(frame, cache, client)
    assert first is not None and first == second
    assert calls == len(frame.tiles())
    assert route.call_count == calls  # the second render came off disk
    image = Image.open(io.BytesIO(first))
    assert image.size == (800, 520)


@respx.mock
async def test_plain_tiles_make_a_1x_picture(tmp_path: Path) -> None:
    respx.get(url__regex=r"https://tiles\.example\.com/.*").mock(
        return_value=httpx.Response(200, content=_png("#336699", side=256))
    )
    frame = basemap.fit([GETTY], 400, 260)
    cache = basemap.TileCache(tmp_path, TILES, ttl_days=30)
    async with httpx.AsyncClient() as client:
        picture = await basemap.render(frame, cache, client)
    assert picture is not None
    assert Image.open(io.BytesIO(picture)).size == (400, 260)


@respx.mock
async def test_a_failed_tile_means_no_basemap(tmp_path: Path) -> None:
    respx.get(url__regex=r"https://tiles\.example\.com/.*").mock(
        return_value=httpx.Response(503)
    )
    frame = basemap.fit([GETTY], 400, 260)
    cache = basemap.TileCache(tmp_path, TILES, ttl_days=30)
    async with httpx.AsyncClient() as client:
        assert await basemap.render(frame, cache, client) is None


@respx.mock
async def test_a_watermark_tile_means_no_basemap(tmp_path: Path) -> None:
    # What CARTO sends for a missing or wrong API key: a 200 and a picture.
    respx.get(url__regex=r"https://tiles\.example\.com/.*").mock(
        return_value=httpx.Response(
            200, content=_png("#eeeeee"), headers={"etag": '"wm-da89c20e77c1-light"'}
        )
    )
    frame = basemap.fit([GETTY], 400, 260)
    cache = basemap.TileCache(tmp_path, TILES, ttl_days=30)
    async with httpx.AsyncClient() as client:
        assert await basemap.render(frame, cache, client) is None
    assert not cache.path(frame.zoom, *frame.tiles()[0]).exists()


def test_positron_with_a_carto_key_else_openstreetmap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("CARTO_API_KEY", "DATE_PLANNER_MAP_TILES_LIGHT"):
        monkeypatch.delenv(name, raising=False)
    plain = Settings(_env_file=None)  # type: ignore[call-arg]
    assert plain.light_tiles() == "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    assert plain.map_credit() == "© OpenStreetMap contributors"

    keyed = Settings(_env_file=None, CARTO_API_KEY="k/1&2")  # type: ignore[call-arg]
    assert keyed.light_tiles() == (
        "https://basemaps.cartocdn.com/rastertiles/light_all/{z}/{x}/{y}@2x.png"
        "?key=k%2F1%262"
    )
    assert (
        keyed.light_tiles()
        .format(z=1, x=2, y=3)
        .endswith("/1/2/3@2x.png?key=k%2F1%262")
    )
    assert keyed.map_credit() == "© OpenStreetMap contributors © CARTO"

    # An explicit template wins, and empty still means no basemap.
    own = Settings(_env_file=None, CARTO_API_KEY="k", map_tiles_light=TILES)  # type: ignore[call-arg]
    assert own.light_tiles() == TILES
    assert own.map_credit() == "© OpenStreetMap contributors"
    off = Settings(_env_file=None, CARTO_API_KEY="k", map_tiles_light="")  # type: ignore[call-arg]
    assert off.light_tiles() == ""


# --- the interactive map's tiles -------------------------------------------

DAYS = {
    "griffith": [GRIFFITH, MARKET, ANGELS],
    "malibu": [SOLSTICE, GETTY, NOBU],
    "one stop": [GETTY],
    # Far apart: the frame zooms right out.
    "la to sf": [GETTY, (37.7749, -122.4194)],
}


@pytest.mark.parametrize("points", DAYS.values(), ids=DAYS.keys())
def test_the_pyramid_stays_within_budget(points: list[tuple[float, float]]) -> None:
    frame = basemap.fit(points, 400, 260)
    pyramid = basemap.plan(frame, points)
    assert len(pyramid.tiles) <= basemap.TILE_BUDGET
    assert len(set(pyramid.tiles)) == len(pyramid.tiles)
    assert pyramid.min_zoom == frame.zoom - 1
    assert frame.zoom <= pyramid.full_zoom <= pyramid.max_zoom <= basemap.DEEPEST
    assert pyramid.max_zoom - pyramid.full_zoom <= basemap.MAX_UPSCALE
    zooms = {z for z, _, _ in pyramid.tiles}
    assert zooms == set(range(pyramid.min_zoom, pyramid.max_zoom + 1))


def _tile_bounds(zoom: int, x: int, y: int) -> basemap.Bounds:
    north, west = basemap.lat_lng(x * 256, y * 256, zoom)
    south, east = basemap.lat_lng((x + 1) * 256, (y + 1) * 256, zoom)
    return south, west, north, east


def _overlaps(a: basemap.Bounds, b: basemap.Bounds) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


@pytest.mark.parametrize("points", DAYS.values(), ids=DAYS.keys())
def test_every_view_the_map_allows_is_tiled(points: list[tuple[float, float]]) -> None:
    """Up to `full_zoom`, every tile touching the bounds is there, and the
    view zoomed right out (wider than the bounds) is too."""
    frame = basemap.fit(points, 400, 260)
    pyramid = basemap.plan(frame, points)
    have = set(pyramid.tiles)
    for zoom in range(pyramid.min_zoom, pyramid.full_zoom + 1):
        size = 2**zoom
        south, west, north, east = pyramid.bounds
        x0, y0 = basemap.world_px(north, west, zoom)
        x1, y1 = basemap.world_px(south, east, zoom)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        half_w = max(x1 - x0, basemap.VIEW_W) / 2
        half_h = max(y1 - y0, basemap.VIEW_H) / 2
        for ty in range(int((cy - half_h) // 256), int((cy + half_h - 1) // 256) + 1):
            for tx in range(
                int((cx - half_w) // 256), int((cx + half_w - 1) // 256) + 1
            ):
                if 0 <= ty < size:
                    assert (zoom, tx, ty) in have, (zoom, tx, ty)


@pytest.mark.parametrize("points", DAYS.values(), ids=DAYS.keys())
def test_deep_zooms_have_every_stop_in_a_patch(
    points: list[tuple[float, float]],
) -> None:
    frame = basemap.fit(points, 400, 260)
    pyramid = basemap.plan(frame, points)
    have = set(pyramid.tiles)
    for zoom in range(pyramid.full_zoom + 1, pyramid.max_zoom + 1):
        patches = [p for p in pyramid.patches if p.zoom == zoom]
        for lat, lng in points:
            # Well inside one patch, not on its edge.
            inside = [
                p
                for p in patches
                if p.bounds[0] < lat < p.bounds[2] and p.bounds[1] < lng < p.bounds[3]
            ]
            assert len(inside) >= 1
        for patch in patches:
            assert set(patch.tiles) <= have
            # Leaflet asks for exactly the tiles the bounds touch.
            touched = {
                (zoom, x, y)
                for _, x0, y0 in patch.tiles
                for x in range(x0 - 1, x0 + 2)
                for y in range(y0 - 1, y0 + 2)
                if _overlaps(patch.bounds, _tile_bounds(zoom, x, y))
            }
            assert touched == set(patch.tiles)


def test_the_griffith_day_zooms_to_street_level() -> None:
    points = DAYS["griffith"]
    frame = basemap.fit(points, 400, 260)
    pyramid = basemap.plan(frame, points)
    assert (pyramid.min_zoom, pyramid.full_zoom, pyramid.max_zoom) == (10, 13, 15)
    # The two downtown stops share their patches.
    assert len(pyramid.patches) == 4


@respx.mock
async def test_the_pyramid_is_fetched_as_webp(tmp_path: Path) -> None:
    route = respx.get(url__regex=r"https://tiles\.example\.com/.*").mock(
        return_value=httpx.Response(200, content=_png("#336699"))
    )
    points = DAYS["griffith"]
    pyramid = basemap.plan(basemap.fit(points, 400, 260), points)
    cache = basemap.TileCache(tmp_path, TILES, ttl_days=30)
    async with httpx.AsyncClient() as client:
        tiles = await basemap.fetch(pyramid, cache, client)
    assert tiles is not None
    assert route.call_count == len(pyramid.tiles)
    assert set(tiles) == {f"tiles/{z}/{x}/{y}.webp" for z, x, y in pyramid.tiles}
    image = Image.open(io.BytesIO(next(iter(tiles.values()))))
    # @2x tiles are halved to the 256px Leaflet shows.
    assert (image.format, image.size) == ("WEBP", (256, 256))
    assert sum(map(len, tiles.values())) < 3_000_000


@respx.mock
async def test_a_rerender_reuses_each_webp_until_its_source_changes(
    tmp_path: Path,
) -> None:
    respx.get(url__regex=r"https://tiles\.example\.com/.*").mock(
        return_value=httpx.Response(200, content=_png("#336699"))
    )
    pyramid = basemap.plan(basemap.fit([GETTY], 400, 260), [GETTY])
    cache = basemap.TileCache(tmp_path, TILES, ttl_days=30)
    async with httpx.AsyncClient() as client:
        first = await basemap.fetch(pyramid, cache, client)
        z, x, y = pyramid.tiles[0]
        converted = cache.converted(z, x, y)
        made_at = converted.stat().st_mtime_ns
        assert await basemap.fetch(pyramid, cache, client) == first
        assert converted.stat().st_mtime_ns == made_at

        # A refetched source is newer than its WebP, which is made again.
        cache.store(z, x, y, _png("#993333"))
        later = converted.stat().st_mtime + 10
        os.utime(cache.path(z, x, y), (later, later))
        again = await basemap.fetch(pyramid, cache, client)
    assert first is not None and again is not None
    name = f"tiles/{z}/{x}/{y}.webp"
    assert again[name] != first[name]
    assert {k: v for k, v in again.items() if k != name} == {
        k: v for k, v in first.items() if k != name
    }


@respx.mock
async def test_a_failed_tile_means_no_pyramid(tmp_path: Path) -> None:
    respx.get(url__regex=r"https://tiles\.example\.com/.*").mock(
        side_effect=[httpx.Response(200, content=_png("#336699"))] * 5
        + [httpx.Response(503)] * 500
    )
    pyramid = basemap.plan(basemap.fit([GETTY], 400, 260), [GETTY])
    cache = basemap.TileCache(tmp_path, TILES, ttl_days=30)
    async with httpx.AsyncClient() as client:
        assert await basemap.fetch(pyramid, cache, client) is None


# --- the page --------------------------------------------------------------


def _stop(number: int, **fields: Any) -> page.PreviewStop:
    values: dict[str, Any] = {
        "number": number,
        "title": f"Stop {number}",
        "venue_name": None,
        "address": None,
        "phone": None,
        "lat": None,
        "lng": None,
        "start_at": None,
        "end_at": None,
        "travel_min_from_prev": None,
        "travel_mode": TravelMode.DRIVING,
        "booking_status": StopBookingStatus.NOT_NEEDED,
        "booking_url": None,
        "confirmation_number": None,
        "on_calendar": False,
        "place": None,
    }
    values.update(fields)
    return page.PreviewStop(**values)


def _data(stops: list[page.PreviewStop], **fields: Any) -> page.PreviewData:
    values: dict[str, Any] = {
        "title": "Malibu day",
        "day": date(2026, 10, 24),
        "tz_label": "PDT (America/Los_Angeles)",
        "status": DateStatus.BOOKING_PENDING,
        "max_leg_minutes": 45,
        "sunset": datetime(2026, 10, 24, 18, 10, tzinfo=LA),
        "stops": stops,
        "map_credit": "© OpenStreetMap contributors",
        "map_width": 400,
        "map_height": 260,
        "has_light_map": True,
        "has_dark_map": True,
    }
    values.update(fields)
    return page.PreviewData(**values)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 24, hour, minute, tzinfo=LA)


def test_the_page_reads_as_the_day_in_order() -> None:
    html = page.render(
        _data(
            [
                _stop(
                    1,
                    title="Solstice Canyon hike",
                    venue_name="Solstice Canyon",
                    address="3800 Solstice Canyon Rd, Malibu, CA 90265",
                    lat=SOLSTICE[0],
                    lng=SOLSTICE[1],
                    start_at=at(9),
                    end_at=at(11),
                    place=(0.1, 0.5),
                    on_calendar=True,
                ),
                _stop(
                    2,
                    title="Getty Villa",
                    venue_name="Getty Villa",
                    lat=GETTY[0],
                    lng=GETTY[1],
                    start_at=at(13),
                    end_at=at(15),
                    travel_min_from_prev=22,
                    booking_status=StopBookingStatus.CONFIRMED,
                    confirmation_number="GV-77",
                    place=(0.9, 0.4),
                ),
                _stop(
                    3,
                    title="Sunset dinner",
                    venue_name="Nobu Malibu",
                    phone="+13103179140",
                    start_at=at(18, 30),
                    end_at=at(20),
                    travel_min_from_prev=18,
                    booking_status=StopBookingStatus.REMINDER_SET,
                ),
            ]
        )
    )
    assert html.startswith("<title>Malibu day</title>")
    assert "Sat, Oct 24 · 9:00 AM–8:00 PM · sunset 6:10 PM" in html
    assert '<span class="chip todo">Bookings pending</span>' in html
    # Times, legs and sunset in order: sunset falls before the 6:30 dinner.
    order = ["9:00 AM", "22 min drive", "1:00 PM", "18 min drive", "Sunset 6:10 PM"]
    positions = [html.index(text) for text in order]
    assert positions == sorted(positions)
    assert html.index("Sunset 6:10 PM") < html.index("6:30 PM")
    assert "Booked" in html and "#GV-77" in html
    assert "Call to book" in html and "(310) 317-9140" in html
    # Two pins and a route; the third stop is named as off the map.
    assert html.count('class="pin"') == 2
    assert "<polyline" in html
    assert "Not on the map yet: Sunset dinner" in html
    assert ">Route in Apple Maps</a>" in html
    assert 'src="map-light.jpg"' in html and 'src="map-dark.jpg"' in html
    assert "© OpenStreetMap contributors" in html
    assert "Open in Google Calendar" in html
    # No Google Maps left anywhere.
    assert "google.com/maps" not in html


def _needed(**fields: Any) -> str:
    stop = _stop(1, booking_status=StopBookingStatus.NEEDED, **fields)
    return page._booking(stop)


def test_a_phone_stop_still_needed_offers_a_call() -> None:
    chip = _needed(channel=BookingChannel.PHONE, phone="(805) 965-6074")
    assert 'href="tel:8059656074"' in chip
    assert "Call to book" in chip and "(805) 965-6074" in chip
    assert "Needs booking" not in chip


def test_a_phone_number_with_a_plus_keeps_it_in_the_tel_link() -> None:
    assert page.tel_link("+1 (310) 317-9140") == "tel:+13103179140"
    assert page.tel_link("call us") is None


def test_a_us_number_reads_as_people_write_it() -> None:
    assert page.phone_label("+13103179140") == "(310) 317-9140"
    assert page.phone_label("805.965.6074") == "(805) 965-6074"
    # Anything else is shown as it was saved.
    assert page.phone_label("+44 20 7946 0958") == "+44 20 7946 0958"
    assert page.phone_label("ext. 12") == "ext. 12"


def test_a_phone_stop_with_no_number_stays_plain() -> None:
    chip = _needed(channel=BookingChannel.PHONE)
    assert "Needs booking" in chip and "href" not in chip


def test_an_online_stop_still_needed_offers_its_link() -> None:
    link = "https://resy.com/cities/los-angeles-ca/venues/a-ti?seats=2"
    chip = _needed(channel=BookingChannel.RESY, book_url=link)
    assert f'href="{link}"' in chip
    assert "Book now" in chip


def test_a_stop_with_no_way_to_book_stays_plain() -> None:
    assert "Needs booking" in _needed(channel=BookingChannel.UNKNOWN)
    assert "Needs booking" in _needed()


def test_the_reminder_chip_dials_too() -> None:
    stop = _stop(
        1,
        booking_status=StopBookingStatus.REMINDER_SET,
        phone="+13103179140",
    )
    assert 'href="tel:+13103179140"' in page._booking(stop)


def test_the_page_leaves_out_what_needs_no_attention() -> None:
    html = page.render(
        _data(
            [
                _stop(
                    1,
                    title="Mount Hollywood hike",
                    venue_name="Mount Hollywood Trail (Griffith Observatory)",
                    address="2800 E Observatory Rd, Los Angeles, CA 90027",
                    lat=GRIFFITH[0],
                    lng=GRIFFITH[1],
                    start_at=at(9, 30),
                    end_at=at(11, 45),
                    place=(0.3, 0.2),
                )
            ],
            status=DateStatus.CONFIRMED,
        )
    )
    # No eyebrow date, stop count, status chip or "no booking" chip...
    assert "Saturday" not in html and "1 stop" not in html
    assert "chip" not in html.split("<main", 1)[1]
    assert "No booking needed" not in html
    # ...nor the venue line, the address or "On calendar".
    assert (
        "Mount Hollywood Trail (Griffith Observatory)"
        not in html.split('<ol class="day">')[1]
    )
    assert "2800 E Observatory Rd" not in html
    assert "On calendar" not in html and "Open in Google Calendar" not in html
    assert "Nothing is booked from this page" not in html
    # The stop keeps its hours, number, title and Apple Maps link.
    assert "9:30 AM" in html and "11:45 AM" in html
    assert ">Apple Maps</a>" in html


def test_the_calendar_button_opens_the_day() -> None:
    html = page.render(_data([_stop(1, on_calendar=False), _stop(2, on_calendar=True)]))
    assert html.count("Open in Google Calendar") == 1
    assert 'href="https://calendar.google.com/calendar/r/day/2026/10/24"' in html
    assert page.google_calendar_day(date(2026, 1, 5)).endswith("/r/day/2026/1/5")


# --- Apple Maps --------------------------------------------------------------


def _params(url: str) -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(url).query)


def test_a_place_link_pins_the_stop_under_its_name() -> None:
    url = page.apple_place(
        _stop(
            1,
            title="Lunch",
            venue_name="Grand Central Market & Co",
            lat=MARKET[0],
            lng=MARKET[1],
        )
    )
    assert url == (
        "https://maps.apple.com/place?coordinate=34.0508,-118.2489"
        "&name=Grand%20Central%20Market%20%26%20Co"
    )
    # No venue yet: the stop's own title names the pin.
    titled = page.apple_place(_stop(1, title="Angels Flight", lat=1.5, lng=2.5))
    assert titled is not None
    assert _params(titled) == [("coordinate", "1.5,2.5"), ("name", "Angels Flight")]


def test_a_place_link_falls_back_to_the_address() -> None:
    url = page.apple_place(_stop(1, address="317 S Broadway, Los Angeles, CA 90013"))
    assert url == (
        "https://maps.apple.com/place?"
        "address=317%20S%20Broadway,%20Los%20Angeles,%20CA%2090013"
    )
    assert page.apple_place(_stop(1)) is None


def test_the_route_runs_through_every_stop_in_order() -> None:
    stops = [
        _stop(1, lat=GRIFFITH[0], lng=GRIFFITH[1]),
        _stop(2),  # no coordinates: skipped
        _stop(3, lat=MARKET[0], lng=MARKET[1]),
        _stop(4, lat=ANGELS[0], lng=ANGELS[1], travel_mode=TravelMode.WALKING),
        _stop(5, lat=GETTY[0], lng=GETTY[1]),
    ]
    url = page.apple_route(stops)
    assert url is not None
    assert url.startswith("https://maps.apple.com/directions?")
    assert _params(url) == [
        ("source", "34.1195,-118.3001"),
        ("destination", "34.0459,-118.5648"),
        ("waypoint", "34.0508,-118.2489"),
        ("waypoint", "34.0513,-118.2497"),
        ("mode", "driving"),
    ]
    # Commas stay literal, as in Apple's examples.
    assert "source=34.1195,-118.3001&" in url


def test_the_route_walks_only_when_every_leg_does() -> None:
    walk = TravelMode.WALKING
    stops = [
        _stop(1, lat=MARKET[0], lng=MARKET[1]),
        _stop(2, lat=ANGELS[0], lng=ANGELS[1], travel_mode=walk),
    ]
    url = page.apple_route(stops)
    assert url is not None and url.endswith("&mode=walking")
    assert "waypoint" not in url
    # The first stop's own mode is how you got there; it doesn't count.
    mixed = [*stops, _stop(3, lat=GETTY[0], lng=GETTY[1])]
    assert (page.apple_route(mixed) or "").endswith("&mode=driving")


def test_one_located_stop_has_no_route() -> None:
    assert page.apple_route([_stop(1, lat=1.0, lng=2.0), _stop(2)]) is None


# --- the interactive map -----------------------------------------------------


def _griffith_data(**fields: Any) -> page.PreviewData:
    points = DAYS["griffith"]
    frame = basemap.fit(points, 400, 260)
    stops = [
        _stop(
            n,
            title=title,
            lat=lat,
            lng=lng,
            start_at=at(9 + n),
            place=frame.place(lat, lng),
        )
        for n, (title, (lat, lng)) in enumerate(
            zip(["Hike", "Market", "Angels Flight</script>"], points), start=1
        )
    ]
    values: dict[str, Any] = {
        "tiles": basemap.plan(frame, points),
        "map_zoom": frame.zoom,
    }
    values.update(fields)
    return _data(stops, **values)


def _config(html: str) -> dict[str, Any]:
    match = re.search(
        r'<script type="application/json" id="live-map-data">(.*?)</script>',
        html,
        re.S,
    )
    assert match
    config: dict[str, Any] = json.loads(match.group(1))
    return config


def test_leaflet_comes_pinned_from_cdnjs_with_its_tiles() -> None:
    data = _griffith_data()
    html = page.render(data)
    assert (
        '<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"'
        f' integrity="{page.LEAFLET_SRI}"'
    ) in html
    assert page.LEAFLET_SRI.startswith("sha512-")
    # The stylesheet is inlined, not linked from a CDN.
    assert ".leaflet-pane" in html
    assert "leaflet.css" not in html and "unpkg.com" not in html
    config = _config(html)
    tiles = data.tiles
    assert tiles is not None
    assert config["url"] == "tiles/{z}/{x}/{y}.webp"
    assert (config["minZoom"], config["fullZoom"], config["maxZoom"]) == (
        tiles.min_zoom,
        tiles.full_zoom,
        tiles.max_zoom,
    )
    assert config["bounds"] == pytest.approx(list(tiles.bounds), abs=1e-6)
    assert len(config["patches"]) == len(tiles.patches)
    assert [s["number"] for s in config["stops"]] == [1, 2, 3]
    place = page.apple_place(data.stops[1])
    assert place is not None
    assert escape(place, quote=True) in config["stops"][1]["popup"]
    assert config["stops"][0]["at"] == "10:00 AM"


def test_the_picture_stays_as_the_fallback() -> None:
    html = page.render(_griffith_data())
    # The picture and its pins render first; Leaflet's container sits on top.
    assert 'src="map-light.jpg"' in html
    assert html.count('class="pin"') == 3
    assert html.index('class="pin"') < html.index('id="live-map"')


def test_the_cards_pin_buttons_work_though_the_map_script_runs_first() -> None:
    html = page.render(_griffith_data())
    # The map's script runs before the cards below it are parsed...
    assert html.index(page.LIVE_JS.strip()[:40]) < html.index('data-locate="1"')
    # ...so it can't find their buttons then: it listens on the document.
    assert 'document.addEventListener("click"' in page.LIVE_JS
    assert '.closest("[data-locate]")' in page.LIVE_JS
    per_button = 'querySelectorAll("[data-locate]")) {\n      button.addEventListener'
    assert per_button not in page.LIVE_JS


def test_stop_titles_cannot_close_the_map_script() -> None:
    html = page.render(_griffith_data())
    assert "Angels Flight</script>" not in html
    assert _config(html)["stops"][2]["title"] == "Angels Flight</script>"


def test_without_tiles_there_is_no_leaflet() -> None:
    html = page.render(_griffith_data(tiles=None))
    assert "leaflet.js" not in html and ".leaflet-pane" not in html
    assert 'id="live-map"' not in html
    assert 'src="map-light.jpg"' in html


def test_a_walking_leg_reads_as_a_walk() -> None:
    html = page.render(
        _data(
            [
                _stop(1, start_at=at(17)),
                _stop(
                    2,
                    start_at=at(18),
                    travel_min_from_prev=12,
                    travel_mode=TravelMode.WALKING,
                ),
            ]
        )
    )
    assert "12 min walk" in html
    assert "12 min drive" not in html
    assert page.WALK in html and page.CAR not in html
    # The limit covers walks as well, so the footer doesn't call it a drive.
    assert "Drives are kept" not in html
    assert "Legs between stops are kept under 45 min." in html


def test_without_dark_tiles_the_light_map_is_darkened() -> None:
    html = page.render(_data([_stop(1, place=(0.5, 0.5))], has_dark_map=False))
    assert 'class="tiles light dimmed"' in html
    assert "map-dark.jpg" not in html


def test_user_text_is_escaped() -> None:
    html = page.render(
        _data(
            [
                _stop(
                    1,
                    title='<script>alert("x")</script>',
                    booking_status=StopBookingStatus.LINK_SENT,
                    booking_url='https://resy.com/?a=1&b="2"',
                )
            ],
            title="Tom & Jerry's <date>",
        )
    )
    assert "<script>" not in html
    assert "Tom &amp; Jerry&#x27;s &lt;date&gt;" in html
    assert 'href="https://resy.com/?a=1&amp;b=&quot;2&quot;"' in html


def test_without_a_basemap_the_pins_sit_on_a_grid() -> None:
    html = page.render(
        _data(
            [_stop(1, lat=GETTY[0], lng=GETTY[1], place=(0.5, 0.5))],
            has_light_map=False,
            has_dark_map=False,
        )
    )
    assert "<img" not in html
    assert 'class="frame bare"' in html
    assert "Map tiles unavailable" in html


def test_sunset_after_every_stop_goes_last() -> None:
    html = page.render(_data([_stop(1, start_at=at(10), end_at=at(12))]))
    assert html.index("12:00 PM") < html.index("Sunset 6:10 PM")


# --- writing the folder ----------------------------------------------------


def _griffith_date() -> PlannedDate:
    """The Griffith day as unsaved rows: `write` only reads them."""

    def stop(
        position: int,
        title: str,
        point: tuple[float, float],
        travel: int | None,
        mode: TravelMode,
    ) -> DateStop:
        return DateStop(
            position=position,
            title=title,
            kind=StopKind.ACTIVITY,
            venue=Venue(
                name=title,
                city="Los Angeles",
                venue_type=VenueType.OTHER,
                lat=point[0],
                lng=point[1],
            ),
            start_at=datetime(2026, 10, 17, 9 + 2 * position, tzinfo=LA),
            anchor=Anchor.NONE,
            reservation_need=ReservationNeed.NONE,
            booking_status=StopBookingStatus.NOT_NEEDED,
            calendar_event_id=f"event{position}",
            travel_min_from_prev=travel,
            travel_mode=mode,
        )

    return PlannedDate(
        id=uuid.uuid4(),
        title="Griffith hike + food crawl",
        day=date(2026, 10, 17),
        max_leg_minutes=25,
        status=DateStatus.CONFIRMED,
        stops=[
            stop(0, "Mount Hollywood hike", GRIFFITH, None, TravelMode.DRIVING),
            stop(1, "Grand Central Market", MARKET, 23, TravelMode.DRIVING),
            stop(2, "Angels Flight", ANGELS, 2, TravelMode.WALKING),
        ],
    )


def _needed_stop(**venue: Any) -> DateStop:
    return DateStop(
        position=0,
        title="Dinner",
        kind=StopKind.MEAL,
        venue=Venue(
            name="Venue",
            city="Santa Barbara",
            timezone="America/Los_Angeles",
            **venue,
        ),
        start_at=datetime(2026, 10, 17, 19, tzinfo=LA),
        party_size=2,
        booking_status=StopBookingStatus.NEEDED,
    )


def test_a_needed_stop_gets_a_link_built_from_its_venue() -> None:
    stop = _needed_stop(
        booking_channel=BookingChannel.RESY,
        platform_slug="a-ti",
        platform_city_slug="los-angeles-ca",
    )
    url = preview._book_url(stop)
    assert url == (
        "https://resy.com/cities/los-angeles-ca/venues/a-ti?date=2026-10-17&seats=2"
    )


def test_no_link_when_the_venue_cannot_build_one() -> None:
    # Phone has none; Resy without its slugs can't build one either.
    for channel in (BookingChannel.PHONE, BookingChannel.RESY):
        assert preview._book_url(_needed_stop(booking_channel=channel)) is None


def test_no_link_once_a_stop_is_past_needed() -> None:
    stop = _needed_stop(
        booking_channel=BookingChannel.RESY,
        platform_slug="a-ti",
        platform_city_slug="los-angeles-ca",
    )
    stop.booking_status = StopBookingStatus.CONFIRMED
    assert preview._book_url(stop) is None


@respx.mock
async def test_write_ships_the_tiles_next_to_the_page(tmp_path: Path) -> None:
    respx.get(url__regex=r"https://tiles\.example\.com/.*").mock(
        return_value=httpx.Response(200, content=_png("#336699", side=256))
    )
    planned = _griffith_date()
    settings = Settings(preview_dir=tmp_path, map_tiles_light=TILES)
    async with httpx.AsyncClient() as client:
        written = await preview.write(planned, LA, settings, client)

    tiles = {name for name in written.files if name.startswith("tiles/")}
    assert set(written.files) == tiles | {"map-light.jpg"}
    assert 0 < len(tiles) <= basemap.TILE_BUDGET
    assert all(re.fullmatch(r"tiles/\d+/\d+/\d+\.webp", name) for name in tiles)
    for name, path in written.files.items():
        assert path == tmp_path / str(planned.id) / name and path.is_file()
    assert written.tiles_overview is not None and written.tiles_overview.is_file()
    assert written.tiles_overview.name not in written.files
    html = written.page.read_text()
    assert "leaflet.js" in html
    assert "mode=driving" in html  # one leg walks, the other drives
    assert "Open in Google Calendar" in html

    # Without a basemap the old tiles go, rather than lingering in the folder.
    bare = Settings(preview_dir=tmp_path, map_tiles_light="")
    async with httpx.AsyncClient() as client:
        again = await preview.write(planned, LA, bare, client)
    assert again.files == {} and again.tiles_overview is None
    folder = tmp_path / str(planned.id)
    assert not (folder / "tiles").exists()
    assert not (folder / preview.OVERVIEW).exists()
    assert "leaflet.js" not in again.page.read_text()


# --- the tool --------------------------------------------------------------


async def test_the_tool_writes_the_page_and_keeps_the_link(
    db: async_sessionmaker[AsyncSession],
    seeded: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATE_PLANNER_PREVIEW_DIR", str(tmp_path))
    monkeypatch.setenv("DATE_PLANNER_MAP_TILES_LIGHT", "")
    get_settings.cache_clear()

    async with Client(create_server()) as client:

        async def call(tool: str, **arguments: Any) -> Any:
            return (await client.call_tool(tool, arguments)).data

        planned = await call(
            "create_date",
            couple_id=str(seeded["couple_id"]),
            title="Malibu day",
            day="2026-10-24",
        )
        for title, kind, venue in (
            ("Solstice Canyon", "activity", seeded["solstice_id"]),
            ("Getty Villa", "ticketed", seeded["getty_id"]),
            ("Dinner", "meal", None),
        ):
            stop: dict[str, Any] = {"title": title, "kind": kind}
            if venue:
                stop["venue_id"] = str(venue)
            await call("add_stop", date_id=str(planned.id), stop=stop)

        out = await call("render_date_preview", date_id=str(planned.id))
        assert Path(out.page_path) == tmp_path / str(planned.id) / "index.html"
        assert out.files == {}
        assert out.tiles_overview is None
        assert out.basemap is False
        assert out.stops_on_map == 2
        assert out.missing_coordinates == ["Dinner"]
        assert out.preview_url is None
        html = Path(out.page_path).read_text()
        assert "Solstice Canyon" in html and "Getty Villa" in html
        # The trail's AllTrails page, saved on the venue, becomes a button.
        assert "alltrails.com/trail/us/california/solstice-canyon-trail-2" in html
        assert ">AllTrails</a>" in html

        link = "https://claude.ai/artifact/abc123"
        updated = await call(
            "update_date", date_id=str(planned.id), patch={"preview_url": link}
        )
        assert updated.preview_url == link
        again = await call("render_date_preview", date_id=str(planned.id))
        assert again.preview_url == link
    get_settings.cache_clear()


# --- AllTrails ---------------------------------------------------------------


TRAIL_PAGE = "https://www.alltrails.com/trail/us/california/solstice-canyon-trail-2"


def test_a_trail_links_to_its_alltrails_page() -> None:
    trail = _stop(
        1,
        venue_type=VenueType.TRAIL,
        website="https://www.parks.ca.gov/?page_id=623",
        external_url=TRAIL_PAGE,
    )
    # On the card, beside Apple Maps...
    card = page.render(_data([trail])).split('<ol class="day">')[1]
    assert f'href="{TRAIL_PAGE}" target="_blank" rel="noopener">AllTrails</a>' in card
    # ...and in the pin's popup, in place of the park's homepage.
    popup = page._popup(trail)
    assert ">AllTrails</a>" in popup
    assert ">Website</a>" not in popup and "parks.ca.gov" not in popup


def test_only_an_alltrails_page_gets_the_alltrails_button() -> None:
    assert page.alltrails_link(_stop(1, external_url="alltrails.com/trail/x")) == (
        "https://alltrails.com/trail/x"
    )
    for url in (
        None,
        "https://example.com/trail/x",
        "https://alltrails.com.evil.example/x",
        "javascript:alert(1)",
    ):
        assert page.alltrails_link(_stop(1, external_url=url)) is None
    # Without one, the popup keeps the venue's own site.
    popup = page._popup(_stop(1, website="getty.edu"))
    assert ">Website</a>" in popup and ">AllTrails</a>" not in popup


def _anchors(html: str) -> list[tuple[str, str]]:
    """Every link's href and visible text, in order."""
    return [
        (href, re.sub(r"<[^>]+>", "", text))
        for href, text in re.findall(r'<a [^>]*href="([^"]*)"[^>]*>(.*?)</a>', html)
    ]


@pytest.mark.parametrize(
    "fields",
    [
        # A trail: Apple Maps and AllTrails, never the park's site.
        {
            "venue_type": VenueType.TRAIL,
            "website": "https://www.nps.gov/samo/",
            "external_url": TRAIL_PAGE,
        },
        # A walk-in bar: Apple Maps and its site.
        {"venue_type": VenueType.BAR, "website": "https://rosenthalestatewines.com"},
        # A phone restaurant still to book: the call, Apple Maps, its site.
        {
            "venue_type": VenueType.RESTAURANT,
            "website": "https://www.noburestaurants.com/malibu/home",
            "phone": "+13103179140",
            "channel": BookingChannel.PHONE,
            "booking_status": StopBookingStatus.NEEDED,
        },
        # Ticketed and still to book: Book now, Apple Maps, its site.
        {
            "venue_type": VenueType.MUSEUM,
            "website": "https://www.getty.edu/visit/villa/",
            "book_url": "https://tickets.getty.edu/online/article/villa",
            "booking_status": StopBookingStatus.NEEDED,
        },
        # Booked: the chip is a label, not a link.
        {
            "venue_type": VenueType.RESTAURANT,
            "website": "https://www.malibu-farm.com",
            "booking_status": StopBookingStatus.CONFIRMED,
            "confirmation_number": "MF-1",
        },
    ],
)
def test_the_card_and_the_pin_offer_the_same_actions(fields: dict[str, Any]) -> None:
    stop = _stop(1, lat=SOLSTICE[0], lng=SOLSTICE[1], place=(0.5, 0.5), **fields)
    card = page._stop(stop, live=True)
    popup = page._popup(stop)
    assert _anchors(card) == _anchors(popup)
    assert _anchors(card)  # never an empty row
    booking = page._booking(stop)
    assert booking in card and booking in popup
