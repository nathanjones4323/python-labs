"""The date preview: framing the map, stitching tiles, and the page itself."""

import io
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from date_planner.config import get_settings
from date_planner.db.models import DateStatus, StopBookingStatus, VenueType
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


# --- the page --------------------------------------------------------------


def _stop(number: int, **fields: Any) -> page.PreviewStop:
    values: dict[str, Any] = {
        "number": number,
        "title": f"Stop {number}",
        "venue_name": None,
        "venue_type": None,
        "address": None,
        "phone": None,
        "lat": None,
        "lng": None,
        "start_at": None,
        "end_at": None,
        "travel_min_from_prev": None,
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
                    venue_type=VenueType.TRAIL,
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
    assert "Saturday, October 24" in html
    assert "Bookings pending" in html
    # Times, legs and sunset in order: sunset falls before the 6:30 dinner.
    order = ["9:00 AM", "22 min drive", "1:00 PM", "18 min drive", "Sunset 6:10 PM"]
    positions = [html.index(text) for text in order]
    assert positions == sorted(positions)
    assert html.index("Sunset 6:10 PM") < html.index("6:30 PM")
    assert "Booked" in html and "#GV-77" in html
    assert "Call to book" in html and "+13103179140" in html
    assert "On calendar" in html
    # Two pins and a route; the third stop is named as off the map.
    assert html.count('class="pin"') == 2
    assert "<polyline" in html
    assert "Not on the map yet: Sunset dinner" in html
    assert "google.com/maps/dir/" in html
    assert 'src="map-light.jpg"' in html and 'src="map-dark.jpg"' in html
    assert "© OpenStreetMap contributors" in html


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
        assert out.basemap is False
        assert out.stops_on_map == 2
        assert out.missing_coordinates == ["Dinner"]
        assert out.preview_url is None
        html = Path(out.page_path).read_text()
        assert "Solstice Canyon" in html and "Getty Villa" in html

        link = "https://claude.ai/artifact/abc123"
        updated = await call(
            "update_date", date_id=str(planned.id), patch={"preview_url": link}
        )
        assert updated.preview_url == link
        again = await call("render_date_preview", date_id=str(planned.id))
        assert again.preview_url == link
    get_settings.cache_clear()
