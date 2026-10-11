"""The map of the day: a picture of the area framed on the stops, and the
tiles for panning and zooming around it.

An Artifact page may load scripts from a few CDNs but nothing else from
outside, so Leaflet can run there while the tile server can't be reached.
The server therefore frames the stops and ships two things next to the
page: one stitched image of the frame (`render`), which shows straight away
and stays if Leaflet never loads, and a small pyramid of tiles (`plan`,
`fetch`) covering the frame plus a margin, a few zoom levels deep, that
Leaflet reads as `tiles/{z}/{x}/{y}.webp`. The pins and the route are
drawn by the page on top, so they stay sharp and follow the theme.

Tiles are cached on disk (`preview_dir/.tiles`), fetched two at a time with
an honest User-Agent, as tile servers' usage policies ask, and never past
zoom 16: OpenStreetMap's policy forbids bulk fetching at 17 and up.
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

# The interactive map. A date's pyramid stays under TILE_BUDGET tiles,
# a couple of MB as WebP; the deepest levels go first when it doesn't fit.
TILE_BUDGET = 200
DEEPEST = 16
# How far past the stops' frame, in px at its zoom, the map can be panned.
MARGIN = 96
# The largest map the page shows (its 560px column less the gutters, at the
# frame's aspect), so zoomed right out the view is still all tiles.
VIEW_W, VIEW_H = 560, 364
# Past the deepest level that covers the whole area, only the tiles around
# each stop are fetched, and Leaflet scales that level up elsewhere: by 4x
# at most, beyond which it's a blur.
MAX_UPSCALE = 2
TILE_FORMAT = "webp"


def world_px(lat: float, lng: float, zoom: int) -> tuple[float, float]:
    """Web Mercator: (`lat`, `lng`) in logical pixels of the world at `zoom`."""
    size = TILE * 2**zoom
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    phi = math.radians(lat)
    x = (lng + 180.0) / 360.0 * size
    y = (1 - math.log(math.tan(phi) + 1 / math.cos(phi)) / math.pi) / 2 * size
    return x, y


def lat_lng(x: float, y: float, zoom: int) -> tuple[float, float]:
    """The inverse of `world_px`."""
    size = TILE * 2**zoom
    lng = x / size * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / size))))
    return lat, lng


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


# south, west, north, east
Bounds = tuple[float, float, float, float]
TileKey = tuple[int, int, int]  # zoom, x, y


def _bounds(left: float, top: float, right: float, bottom: float, zoom: int) -> Bounds:
    north, west = lat_lng(left, top, zoom)
    south, east = lat_lng(right, bottom, zoom)
    return south, west, north, east


@dataclass(frozen=True)
class Patch:
    """Tiles around one stop at one zoom past `Pyramid.full_zoom`. Leaflet
    shows them as their own layer, limited to `bounds`, so it never asks
    for a tile outside the patch."""

    zoom: int
    bounds: Bounds
    tiles: tuple[TileKey, ...]


@dataclass(frozen=True)
class Pyramid:
    """The tiles the interactive map can show, and the limits that keep it
    to them: zoom from `min_zoom` to `max_zoom`, panning within `bounds`.
    Every level up to `full_zoom` covers everything the view can reach;
    deeper ones only the `patches` around the stops."""

    min_zoom: int
    full_zoom: int
    max_zoom: int
    bounds: Bounds
    patches: tuple[Patch, ...]
    tiles: tuple[TileKey, ...]


def _area(frame: Frame, zoom: int) -> tuple[int, int, int, int]:
    """The tiles (x0, y0, x1, y1, inclusive) the view can reach at `zoom`:
    the frame plus the margin, widened to the largest view when that's
    bigger — Leaflet centres a view that's wider than the bounds."""
    scale = 2.0 ** (zoom - frame.zoom)
    left = (frame.left - MARGIN) * scale
    top = (frame.top - MARGIN) * scale
    right = (frame.left + frame.width + MARGIN) * scale
    bottom = (frame.top + frame.height + MARGIN) * scale
    centre_x, centre_y = (left + right) / 2, (top + bottom) / 2
    half_w = max(right - left, VIEW_W) / 2
    half_h = max(bottom - top, VIEW_H) / 2
    last = 2**zoom - 1
    return (
        math.floor((centre_x - half_w) / TILE),
        max(0, math.floor((centre_y - half_h) / TILE)),
        math.floor((centre_x + half_w - 1) / TILE),
        min(last, math.floor((centre_y + half_h - 1) / TILE)),
    )


def _area_count(frame: Frame, zoom: int) -> int:
    x0, y0, x1, y1 = _area(frame, zoom)
    return (x1 - x0 + 1) * (y1 - y0 + 1)


def _patches(points: list[tuple[float, float]], zoom: int) -> list[Patch]:
    """2x2 tiles around each point, split at the tile corner nearest it, so
    the point sits in the middle half of its patch. Points sharing a patch
    share one."""
    patches: dict[tuple[int, int], Patch] = {}
    count = 2**zoom
    for lat, lng in points:
        x, y = world_px(lat, lng, zoom)
        cx = min(max(round(x / TILE), 1), count - 1)
        cy = min(max(round(y / TILE), 1), count - 1)
        if (cx, cy) in patches:
            continue
        # A hair inside the tile edges: Leaflet loads every tile the bounds
        # touch, and these must touch only the four.
        inset = 0.05
        bounds = _bounds(
            (cx - 1) * TILE + inset,
            (cy - 1) * TILE + inset,
            (cx + 1) * TILE - inset,
            (cy + 1) * TILE - inset,
            zoom,
        )
        tiles = tuple((zoom, x, y) for y in (cy - 1, cy) for x in (cx - 1, cx))
        patches[cx, cy] = Patch(zoom, bounds, tiles)
    return list(patches.values())


def plan(frame: Frame, points: list[tuple[float, float]]) -> Pyramid:
    """The deepest pyramid within `TILE_BUDGET`: as many zoom levels as fit,
    and of those, as many as possible covering the whole area."""
    min_zoom = max(0, frame.zoom - 1)
    deepest = max(frame.zoom, DEEPEST)
    patches = {z: _patches(points, z) for z in range(frame.zoom + 1, deepest + 1)}
    choice = (frame.zoom, frame.zoom)
    for max_zoom in range(deepest, frame.zoom - 1, -1):
        found = None
        for full_zoom in range(
            max_zoom, max(frame.zoom, max_zoom - MAX_UPSCALE) - 1, -1
        ):
            count = sum(_area_count(frame, z) for z in range(min_zoom, full_zoom + 1))
            count += sum(
                len(p.tiles)
                for z in range(full_zoom + 1, max_zoom + 1)
                for p in patches[z]
            )
            if count <= TILE_BUDGET:
                found = (full_zoom, max_zoom)
                break
        if found:
            choice = found
            break
    full_zoom, max_zoom = choice

    tiles: list[TileKey] = []
    for z in range(min_zoom, full_zoom + 1):
        x0, y0, x1, y1 = _area(frame, z)
        tiles += [(z, x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
    deep = tuple(p for z in range(full_zoom + 1, max_zoom + 1) for p in patches[z])
    tiles += [t for p in deep for t in p.tiles]
    return Pyramid(
        min_zoom=min_zoom,
        full_zoom=full_zoom,
        max_zoom=max_zoom,
        bounds=_bounds(
            frame.left - MARGIN,
            frame.top - MARGIN,
            frame.left + frame.width + MARGIN,
            frame.top + frame.height + MARGIN,
            frame.zoom,
        ),
        patches=deep,
        tiles=tuple(dict.fromkeys(tiles)),
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
    # CARTO answers a missing or wrong API key with a watermark tile, not
    # an error; a map of those is worse than none.
    if response.headers.get("etag", "").startswith('"wm-'):
        raise httpx.HTTPStatusError(
            "watermark tile (API key missing or wrong)",
            request=response.request,
            response=response,
        )
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


async def fetch(
    pyramid: Pyramid,
    cache: TileCache,
    client: httpx.AsyncClient,
) -> dict[str, bytes] | None:
    """The pyramid's tiles as WebP, keyed by their path next to the page
    (`tiles/{z}/{x}/{y}.webp`), or None if any tile failed: the map would
    have holes, and the page keeps its picture instead."""
    gate = asyncio.Semaphore(2)
    keys = [(z, x % 2**z, y) for z, x, y in pyramid.tiles]
    keys = list(dict.fromkeys(keys))
    try:
        images = await asyncio.gather(
            *(_tile(client, cache, gate, z, x, y) for z, x, y in keys)
        )
    except httpx.HTTPError:
        return None
    out: dict[str, bytes] = {}
    for (z, x, y), data in zip(keys, images):
        try:
            tile = Image.open(io.BytesIO(data)).convert("RGB")
        except OSError:
            return None
        # @2x tiles are halved: Leaflet shows 256px tiles, and four times
        # the pixels would be four times the bytes.
        if tile.size != (TILE, TILE):
            tile = tile.resize((TILE, TILE), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        tile.save(buffer, "WEBP", quality=80, method=6)
        out[f"tiles/{z}/{x}/{y}.{TILE_FORMAT}"] = buffer.getvalue()
    return out
