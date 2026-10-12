# date-planner

An MCP server for planning a couple's dates, plus the Claude Code skill that
drives it.

A date is a **day with an ordered list of stops**: a hike, a timed museum
entry, a sunset dinner. The server owns the parts that want code — the
couple's preferences and past dates, venues and how each one takes
reservations, prefilled booking links, booking windows, sunset, travel time
between stops, and a deterministic timeline checker. The skill
(`.claude/skills/date-planner/SKILL.md`) owns the judgement: which stops, in
what order, what "nearby" means today. It uses the official Google Calendar
and Gmail connectors for everything Google.

**It never books anything.** For an online platform it produces a link for
the user to tap; for a phone-only venue it works out when the lines open and
leaves a reminder with the number and a script; for timed entry it links the
venue's own ticket page. Google credentials stay with the connectors, and the
final step stays with a person.

## Quick start

```sh
cd apps/date-planner
cp .env.example .env            # the defaults match the compose file
docker compose up -d            # Postgres on 5434, plus a test database
uv sync --package date-planner
uv run --package date-planner alembic upgrade head
uv run --package date-planner python -m date_planner.seed
```

The seed prints the couple id and inserts the venues for a Malibu day:
Solstice Canyon (trail, trailhead coordinates), the Getty Villa (museum,
timed entry) and Nobu Malibu (phone-only, bookable one calendar month ahead
from 10:00 local). Set `DATE_PLANNER_SEED_MEMBER_EMAILS` (comma-separated) and
`DATE_PLANNER_SHARED_CALENDAR_ID` in `.env` before seeding; a reseed with them
unset overwrites the stored values.

## Claude Code project

Open Claude Code **in `apps/date-planner`** — that's the project root the
config below lives in:

| file | what it does |
| --- | --- |
| `.mcp.json` | Registers this server (`uv run date-planner-mcp`). `DATABASE_URL` comes from your environment, falling back to the local compose database. |
| `.claude/skills/date-planner/SKILL.md` | The skill, invocable as `/date-planner`. |
| `.claude/settings.json` | `PreToolUse` hooks that enforce the hard rules in code. |
| `.claude/hooks/calendar_guard.py` | Denies calendar deletes; asks before any create/update that adds attendees; allows the rest. |
| `.claude/hooks/deny.py` | Denies with a fixed reason — used to block sending email. |

Claude Code asks you to approve a project `.mcp.json` server the first time.

### Google connectors

Google Calendar and Gmail come from the claude.ai connectors, which Claude
Code shows under `/mcp`. Their tool names, as of 2026-10-11, which the hook
matchers in `.claude/settings.json` are written against (and
`tests/test_hooks.py` checks):

| connector | tools the skill uses | tools the hooks gate |
| --- | --- | --- |
| Google Calendar | `mcp__claude_ai_Google_Calendar__list_events`, `__suggest_time`, `__create_event`, `__update_event` | `__create_event`, `__update_event` (ask if attendees), `__delete_event` (deny) |
| Gmail | `mcp__claude_ai_Gmail__search_threads`, `__get_thread` | `__send_message`, `__reply`, `__forward` (deny) |
| AllTrails (optional) | `mcp__claude_ai_AllTrails__search_trails_by_name`, `__get_trail_details` | — |

The calendar guard treats any non-empty field with "attendee" or "guest" in
its name as an invite: `attendees` / `attendeeEmails` on `create_event`,
`addedAttendees` / `addedAttendeeEmails` on `update_event`.
`guestPermissions` and `removedAttendeeEmails` invite nobody and pass.

### Without the project config

To use the server from another directory:

```sh
claude mcp add date-planner \
  --env DATABASE_URL=postgresql://postgres:postgres@localhost:5434/date_planner \
  -- uv run --project /absolute/path/to/python-labs \
     --package date-planner date-planner-mcp
```

The hooks only apply in sessions started in `apps/date-planner`.

### HTTP transport

`date-planner-mcp --http` serves streamable HTTP on `127.0.0.1:8002` for use
as a custom connector later. It refuses to start without
`DATE_PLANNER_HTTP_TOKEN`, which clients send as `Authorization: Bearer
<token>`. One static token bound to localhost is the whole auth story — it is
not ready for public hosting.

## The tools

| Tool | What it's for |
| --- | --- |
| `list_couples`, `get_couple_profile`, `upsert_couple_profile` | The couple and their preferences |
| `edit_couple_preferences` | Add or drop single likes, dislikes or dietary needs without resending the list |
| `list_past_dates` | Recent dates with ratings and every stop, so no venue repeats |
| `find_venue`, `upsert_venue` | Venues of any type: restaurant, trail, museum, bar, park, viewpoint |
| `detect_booking_channel` | Read the venue's website: Resy / OpenTable / Tock / SevenRooms / ticketed / phone / walk-in / unknown, with evidence and a confidence |
| `suggest_reservation_need` | The default need for a stop, from its venue's channel |
| `build_booking_link` | A stop's reservation link: prefilled for the platforms, the venue's own page for ticketed |
| `compute_booking_window` | When bookings open for a stop's time, in the venue's timezone |
| `create_date`, `update_date`, `get_date`, `list_dates` | The day itself |
| `add_stop`, `update_stop`, `reorder_stops`, `remove_stop` | Its stops |
| `get_sunset` | Sunset, civil dusk, golden hour — computed locally with `astral` |
| `estimate_travel` | One leg's time and distance, cached |
| `plan_timeline` | Place every stop in the current order and report what's wrong |
| `render_date_preview` | Write the day as a phone-sized page: a map of the stops and the route you can pan and zoom, then the timeline with drives, sunset, bookings still to make and Apple Maps links |

### Status

A date's status is **derived** from its stops and stored after every stop
change: any stop `needed` / `link_sent` / `reminder_set` makes it
`booking_pending`; every stop `not_needed` or `confirmed`, with times, makes
it `confirmed`; anything else is `planning`. `completed` and `cancelled` are
set by hand (`update_date`) and stick.

### The timeline

`plan_timeline(date_id, buffer_min=10, apply=False)` never reorders. Fixed
stops keep their time; sunset stops go at sunset + offset (default −30),
rounded to 15 minutes; flexible stops walk backward from the first anchor and
forward from each one after, each leg costing travel + buffer. It reports
`leg_exceeds_max`, `overlap`, `outside_window`, `missing_duration`,
`missing_coordinates` and `daylight` (a trail ending after sunset), and
writes nothing unless `apply=True`.

## Travel between stops

`TRAVEL_PROVIDER` picks how legs are timed. **Nothing paid is needed.**

| provider | how | notes |
| --- | --- | --- |
| `haversine` (default) | straight line × 1.4, at 40 km/h driving / 4.8 km/h walking | Offline, free. Pessimistic on fast roads: Solstice Canyon → Getty Villa comes out at 43 min padded, where OSRM routes it at 23.7 min (18.5 km) unpadded — checked once against the demo server on 2026-10-11. |
| `osrm` | `GET {OSRM_URL}/route/v1/{profile}/…` | Self-hosted, below. |
| `ors` | OpenRouteService directions with `ORS_API_KEY` | Free tier; hosted. |
| `google` | — | Placeholder for traffic-aware times; raises `NotImplementedError`. |

None of them know about traffic, so every driving time gets
`TRAVEL_PADDING_PCT` (default 20%) on top. Results are cached per direction,
mode and provider for 30 days in `travel_cache`, unpadded, so changing the
padding takes effect immediately.

### Road routing with OSRM

```sh
mkdir -p data/osrm && cd data/osrm
curl -LO https://download.geofabrik.de/north-america/us/california/socal-latest.osm.pbf
IMG=ghcr.io/project-osrm/osrm-backend:v5.27.1
docker run --rm -t -v "$PWD:/data" $IMG osrm-extract -p /opt/car.lua /data/socal-latest.osm.pbf
docker run --rm -t -v "$PWD:/data" $IMG osrm-partition /data/socal-latest.osrm
docker run --rm -t -v "$PWD:/data" $IMG osrm-customize /data/socal-latest.osrm
cd ../.. && docker compose --profile routing up -d osrm
```

Then `TRAVEL_PROVIDER=osrm` (and `OSRM_URL=http://localhost:5000`) in `.env`.
The extract is around 500 MB and preparing it takes a while and several GB
of RAM. Don't point `OSRM_URL` at the public demo server
(`router.project-osrm.org`) other than for a one-off manual check: it's
rate-limited and not for applications.

## The preview page

`render_date_preview` writes `previews/<date id>/index.html` plus the map
under it, and the skill publishes that folder with Claude Code's Artifact
tool, so it opens as a card in the chat — on a phone, through Remote
Control, too. The Artifact's link is saved on the date (`preview_url`) and
every later change republishes to the same link.

An Artifact page can load scripts from cdnjs but no map tiles from another
host, so the server ships the tiles with the page. It stitches the few that
cover the stops into one picture, which shows first, with the pins and the
route drawn on top; then Leaflet (1.9.4, pinned, from cdnjs) takes over
with a small pyramid of tiles published next to the page as
`tiles/{z}/{x}/{y}.webp`: the stops' area plus a margin from one zoom out
to two in, and closer still (to zoom 16 at most) just around each
stop, at most 200 tiles and around 2.5 MB. Panning and zooming are held to
what was fetched, so the map never shows a blank tile; zoomed past the
whole-area levels away from a stop, it's the closest whole-area level
scaled up. If Leaflet doesn't load, the picture stays.

The tiles are CARTO's Positron when `CARTO_API_KEY` is set in `.env`, and
OpenStreetMap's own otherwise. CARTO needs a key since September 2026: it's
free, without an account, from <https://carto.com/basemaps/apikey>, for
non-commercial use up to 5M tiles a month; without one (or with a wrong
one) it answers every tile with an "API key required" watermark, which the
server refuses rather than put on the map. Either way it's light use: a
couple of hundred tiles per date at zoom 16 and below (OSM's policy forbids
bulk fetching at 17+), an identifying User-Agent, two requests at a time,
cached under `previews/.tiles` for 30 days, and the credit on the map. In
dark mode the page darkens the light tiles itself.

`DATE_PLANNER_MAP_TILES_LIGHT` / `_DARK` take any `{z}/{x}/{y}` template,
256px or @2x (set `DATE_PLANNER_MAP_ATTRIBUTION` to match), or an empty
light URL for no basemap: the pins then sit on a plain grid, still to
scale. The dark template only changes the picture; the interactive map
always uses the light tiles, darkened.

Map links open Apple Maps (its unified `maps.apple.com` URLs, iOS 18.4+):
each stop's place card, and the route through every stop in order, walking
when every leg walks and driving otherwise. "Open in Google Calendar" opens
the day on the web calendar: the iPhone app takes no https links, and a
`googlecalendar://` link can't be relied on from the Artifact's sandboxed
frame.

The session needs `Artifact` and `Read(./previews/**)` in its allowlist —
see `.claude/settings.local.json` for Remote Control.

## Booking windows

"One month ahead" is not "30 days ahead". `compute_booking_window` counts
calendar months with `relativedelta`, so a 31 March dinner opens on 28 (or 29)
February, and it combines the open time with the date in the venue's own
timezone, so 10:00 local stays 10:00 across a DST change.

## Deep links are verified

The four platform URL templates were written from the shape of each
platform's public URLs, then checked by hand against live venues on
2026-10-10 — see the note above `links.VERIFIED` for which venue confirmed
each one. `build_booking_link` reports `verified: true` for all four as of
that date, and always `false` for a ticketed venue's own page, which doesn't
prefill.

If a platform changes its URL shape later, `scripts/verify_links.py` prints
fresh links to re-check:

```sh
uv run --package date-planner python scripts/verify_links.py
uv run --package date-planner python scripts/verify_links.py --from-db
```

A 404 or "not found" from the platform itself can mean the sample slug is
stale or the venue isn't currently taking reservations there, rather than the
template being wrong — check the platform's own error message first.

## Migrating from v1

`alembic upgrade head` runs migration `002`, which renames `restaurants` to
`venues` (backfilling `venue_type=restaurant`), turns each v1 date into a day
with one stop carrying its restaurant, times and booking fields, and maps the
statuses across (see the migration's docstring). It's reversible:
`alembic downgrade 001` puts the first stop back onto each date, and v1 data
survives the round trip unchanged (`tests/test_migration.py`).

## Tests

```sh
uv run --package date-planner pytest
```

Database tests need `DATE_PLANNER_TEST_DATABASE_URL` (the compose file creates
`date_planner_test`) and are skipped without it, so the pure tests run
anywhere. The schema is applied with Alembic, so the migrations are exercised
too. `mypy` and `ruff check` should both be clean:

```sh
uv run --package date-planner mypy
uvx ruff check . && uvx ruff format --check .
```

## Not in scope

Automated booking of any kind (platform APIs, browser automation, phone
calls), paid routing APIs, multi-user auth, and public hosting.
