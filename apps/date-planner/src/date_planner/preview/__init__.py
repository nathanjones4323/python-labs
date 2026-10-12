"""A phone-sized preview of a planned date, for publishing as an Artifact.

`write` turns a date into a folder of files — `index.html`, one basemap
image per theme, and the interactive map's tiles under `tiles/` — under
`preview_dir/<date id>/`. The session publishes that folder with the
Artifact tool; this server never publishes anything itself.
"""

import asyncio
import io
import shutil
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from PIL import Image

from date_planner.config import Settings
from date_planner.db.models import DateStop, PlannedDate
from date_planner.planning import sunset
from date_planner.preview import basemap, page

MAP_WIDTH = 400
MAP_HEIGHT = 340
OVERVIEW = "tiles-overview.jpg"


@dataclass(frozen=True)
class Written:
    page: Path
    # Published path -> local file, for the Artifact tool's `files`.
    files: dict[str, Path]
    stops_on_map: int
    missing_coordinates: list[str]
    basemap: bool
    # Every tile in `files` on one sheet, for looking them over before they
    # are published; not published itself.
    tiles_overview: Path | None


def _cache(template: str, settings: Settings) -> basemap.TileCache:
    return basemap.TileCache(
        settings.preview_dir.resolve() / ".tiles", template, settings.map_tile_ttl_days
    )


def _overview(tiles: dict[str, bytes]) -> bytes:
    """The tiles at half size, 16 to a row, in pyramid order."""
    side, columns = basemap.TILE // 2, 16
    rows = -(-len(tiles) // columns)
    sheet = Image.new("RGB", (columns * side, rows * side), "#ffffff")
    for i, data in enumerate(tiles.values()):
        tile = Image.open(io.BytesIO(data)).resize((side, side))
        sheet.paste(tile, (i % columns * side, i // columns * side))
    out = io.BytesIO()
    sheet.save(out, "JPEG", quality=80)
    return out.getvalue()


async def _basemap(
    template: str,
    frame: basemap.Frame,
    settings: Settings,
    client: httpx.AsyncClient,
) -> bytes | None:
    if not template:
        return None
    return await basemap.render(frame, _cache(template, settings), client)


def _minutes(stop: DateStop) -> int | None:
    """How long the stop lasts: its times when both are set, else the estimate."""
    if stop.start_at is not None and stop.end_at is not None:
        minutes = round((stop.end_at - stop.start_at).total_seconds() / 60)
        return minutes if minutes > 0 else None
    return stop.est_duration_min


async def write(
    planned: PlannedDate,
    tz: ZoneInfo,
    settings: Settings,
    client: httpx.AsyncClient,
) -> Written:
    """Render `planned` (stops and venues loaded) into its preview folder."""
    stops = list(planned.stops)
    located = [
        (s.venue.lat, s.venue.lng)
        for s in stops
        if s.venue is not None and s.venue.lat is not None and s.venue.lng is not None
    ]
    frame = basemap.fit(located, MAP_WIDTH, MAP_HEIGHT) if located else None

    light = dark = None
    pyramid = tiles = None
    if frame is not None:
        light = await _basemap(settings.light_tiles(), frame, settings, client)
        if light is not None:
            dark = await _basemap(settings.map_tiles_dark, frame, settings, client)
            # Light tiles only: in dark mode the page darkens them, rather
            # than shipping every tile twice.
            pyramid = basemap.plan(frame, located)
            tiles = await basemap.fetch(
                pyramid, _cache(settings.light_tiles(), settings), client
            )

    day_sunset = (
        sunset.compute(located[0][0], located[0][1], planned.day, tz).sunset
        if located
        else None
    )

    preview_stops = []
    for number, stop in enumerate(stops, start=1):
        venue = stop.venue
        place = None
        if (
            frame is not None
            and venue is not None
            and venue.lat is not None
            and venue.lng is not None
        ):
            place = frame.place(venue.lat, venue.lng)
        preview_stops.append(
            page.PreviewStop(
                number=number,
                title=stop.title,
                venue_name=venue.name if venue else None,
                address=venue.address if venue else None,
                phone=venue.phone if venue else None,
                lat=venue.lat if venue else None,
                lng=venue.lng if venue else None,
                start_at=stop.start_at and stop.start_at.astimezone(tz),
                end_at=stop.end_at and stop.end_at.astimezone(tz),
                travel_min_from_prev=stop.travel_min_from_prev,
                travel_mode=stop.travel_mode,
                booking_status=stop.booking_status,
                booking_url=stop.booking_url,
                confirmation_number=stop.confirmation_number,
                on_calendar=stop.calendar_event_id is not None,
                place=place,
                venue_type=venue.venue_type if venue else None,
                website=venue.website if venue else None,
                duration_min=_minutes(stop),
            )
        )

    noon = datetime.combine(planned.day, time(12), tzinfo=tz)
    html = page.render(
        page.PreviewData(
            title=planned.title,
            day=planned.day,
            tz_label=f"{noon.tzname()} ({tz.key})",
            status=planned.status,
            max_leg_minutes=planned.max_leg_minutes,
            sunset=day_sunset,
            stops=preview_stops,
            map_credit=settings.map_credit(),
            map_width=MAP_WIDTH,
            map_height=MAP_HEIGHT,
            has_light_map=light is not None,
            has_dark_map=dark is not None,
            tiles=pyramid if tiles is not None else None,
            map_zoom=frame.zoom if frame is not None else 0,
        )
    )

    folder = settings.preview_dir.resolve() / str(planned.id)
    folder.mkdir(parents=True, exist_ok=True)
    index = folder / "index.html"
    index.write_text(html, encoding="utf-8")
    files: dict[str, Path] = {}
    for name, image in ((page.LIGHT_MAP, light), (page.DARK_MAP, dark)):
        path = folder / name
        if image is not None:
            path.write_bytes(image)
            files[name] = path
        else:
            # A stale picture from an earlier render would show the old area.
            path.unlink(missing_ok=True)
    shutil.rmtree(folder / "tiles", ignore_errors=True)
    overview = folder / OVERVIEW
    overview.unlink(missing_ok=True)
    if tiles is not None:
        for name, data in tiles.items():
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            files[name] = path
        overview.write_bytes(await asyncio.to_thread(_overview, tiles))

    return Written(
        page=index,
        files=files,
        stops_on_map=sum(1 for s in preview_stops if s.place is not None),
        missing_coordinates=[s.title for s in preview_stops if s.place is None],
        basemap=light is not None,
        tiles_overview=overview if tiles is not None else None,
    )
