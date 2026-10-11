"""A static map of the day: one picture of the area, framed on the stops.

An Artifact page may only load scripts from a few CDNs, so a slippy map
that pulls tiles as you pan can't run there. Instead the server frames the
stops, fetches the few tiles that cover them, and stitches them into one
image the page ships alongside itself. The pins and the route aren't in the
picture: the page draws them on top, from `Frame.place`, so they stay sharp
and follow the theme.

Tiles are cached on disk (`preview_dir/.tiles`), fetched two at a time with
an honest User-Agent, as tile servers' usage policies ask.
"""

import asyncio
import hashlib
import io
import math
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image

TILE = 256  # logical pixels per tile; @2x tiles are twice that
MIN_ZOOM = 3
MAX_ZOOM = 15
# Room around the outermost stops for a pin and its number.
PAD_X = 40
PAD_Y = 44
MAX_LAT = 85.05112878


def world_px(lat: float, lng: float, zoom: int) -> tuple[float, float]:
    """Web Mercator: (`lat`, `lng`) in logical pixels of the world at `zoom`."""
    size = TILE * 2**zoom
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    phi = math.radians(lat)
    x = (lng + 180.0) / 360.0 * size
    y = (1 - math.log(math.tan(phi) + 1 / math.cos(phi)) / math.pi) / 2 * size
    return x, y


@dataclass(frozen=True)
class Frame:
    """The part of the world at `zoom` that the map shows, in logical px."""

    zoom: int
    left: int
    top: int
    width: int
    height: int

    def place(self, lat: float, lng: float) -> tuple[float, float]:
        """Where a point falls on the map, as fractions of its width and
        height (0-1), which the page turns into percentages."""
        x, y = world_px(lat, lng, self.zoom)
        return (x - self.left) / self.width, (y - self.top) / self.height

    def tiles(self) -> list[tuple[int, int]]:
        """The (x, y) tiles that cover the frame, unwrapped: x may run off
        either end of the world and is wrapped when fetched."""
        x0, y0 = self.left // TILE, self.top // TILE
        x1 = (self.left + self.width - 1) // TILE
        y1 = (self.top + self.height - 1) // TILE
        return [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]


def fit(points: list[tuple[float, float]], width: int, height: int) -> Frame:
    """The closest zoom that keeps every point inside the padded frame,
    centred on them. One point gets a neighbourhood-sized view."""
    if not points:
        raise ValueError("fit needs at least one point")
    zoom = MIN_ZOOM
    for candidate in range(MAX_ZOOM, MIN_ZOOM - 1, -1):
        xs, ys = zip(*(world_px(lat, lng, candidate) for lat, lng in points))
        if (
            max(xs) - min(xs) <= width - 2 * PAD_X
            and max(ys) - min(ys) <= height - 2 * PAD_Y
        ):
            zoom = candidate
            break
    if len(set(points)) == 1:
        zoom = min(zoom, 14)
    xs, ys = zip(*(world_px(lat, lng, zoom) for lat, lng in points))
    centre_x, centre_y = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    return Frame(
        zoom=zoom,
        left=round(centre_x - width / 2),
        top=round(centre_y - height / 2),
        width=width,
        height=height,
    )


class TileCache:
    """Tiles on disk, one folder per tile style, reused for `ttl_days`."""

    def __init__(self, root: Path, template: str, ttl_days: int) -> None:
        style = hashlib.sha1(template.encode()).hexdigest()[:10]
        self.root = root / style
        self.template = template
        self.ttl_s = ttl_days * 86_400

    def path(self, zoom: int, x: int, y: int) -> Path:
        return self.root / str(zoom) / str(x) / f"{y}.png"

    def fresh(self, zoom: int, x: int, y: int) -> bytes | None:
        path = self.path(zoom, x, y)
        try:
            if time.time() - path.stat().st_mtime < self.ttl_s:
                return path.read_bytes()
        except FileNotFoundError:
            pass
        return None

    def store(self, zoom: int, x: int, y: int, data: bytes) -> None:
        path = self.path(zoom, x, y)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def url(self, zoom: int, x: int, y: int) -> str:
        return self.template.format(z=zoom, x=x, y=y)


async def _tile(
    client: httpx.AsyncClient,
    cache: TileCache,
    gate: asyncio.Semaphore,
    zoom: int,
    x: int,
    y: int,
) -> bytes:
    cached = cache.fresh(zoom, x, y)
    if cached is not None:
        return cached
    async with gate:
        response = await client.get(cache.url(zoom, x, y))
    response.raise_for_status()
    cache.store(zoom, x, y, response.content)
    return response.content


async def render(
    frame: Frame,
    cache: TileCache,
    client: httpx.AsyncClient,
) -> bytes | None:
    """The frame's basemap as a JPEG, or None if any tile failed —
    a map with holes in it reads as broken, a plain ground doesn't."""
    count = 2**frame.zoom
    gate = asyncio.Semaphore(2)
    wanted = [(x, y) for x, y in frame.tiles() if 0 <= y < count]
    try:
        images = await asyncio.gather(
            *(_tile(client, cache, gate, frame.zoom, x % count, y) for x, y in wanted)
        )
    except httpx.HTTPError:
        return None

    try:
        tiles = [Image.open(io.BytesIO(data)).convert("RGB") for data in images]
    except OSError:
        return None
    # 256px tiles make a 1x picture, @2x tiles a 2x one; the page shows
    # either at the frame's size.
    scale = max(1, tiles[0].width // TILE) if tiles else 1
    side = TILE * scale
    canvas = Image.new("RGB", (frame.width * scale, frame.height * scale), "#dde3e8")
    for (x, y), tile in zip(wanted, tiles):
        if tile.size != (side, side):
            tile = tile.resize((side, side))
        canvas.paste(
            tile,
            ((x * TILE - frame.left) * scale, (y * TILE - frame.top) * scale),
        )
    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=86, optimize=True, progressive=True)
    return out.getvalue()
