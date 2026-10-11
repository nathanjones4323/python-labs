# date-planner

An MCP server for planning a couple's dates, plus the skill that drives it.

The server owns the parts that want code: the couple's preferences and past
dates, restaurants and how each one takes reservations, prefilled booking
links, and the arithmetic of when a booking window opens. The skill
(`skills/date-planner/SKILL.md`) owns the judgement, and uses the official
Google Calendar and Gmail connectors for everything Google.

**It never books anything.** For an online platform it produces a link for
the user to tap; for a phone-only restaurant it works out when the lines open
and leaves a reminder with the number and a script. Google credentials stay
with the connectors, and the final step stays with a person.

## Quick start

```sh
cd apps/date-planner
cp .env.example .env            # the defaults match the compose file
docker compose up -d            # Postgres on 5434, plus a test database
uv sync --package date-planner
uv run --package date-planner alembic upgrade head
uv run --package date-planner python -m date_planner.seed
```

The seed prints the couple id and inserts Nobu Malibu, which is the
interesting case: phone-only, bookable one calendar month ahead from 10:00
local. Edit the placeholder couple with `upsert_couple_profile` (names,
emails, shared calendar id) once you know the real values.

Commands read `.env` from the working directory, so run them from
`apps/date-planner` — or set `DATABASE_URL` in the environment.

## Registering the server with Claude Code

```sh
claude mcp add date-planner \
  --env DATABASE_URL=postgresql://postgres:postgres@localhost:5434/date_planner \
  -- uv run --project /absolute/path/to/python-labs \
     --package date-planner date-planner-mcp
```

Then check it: ask for the `ping` tool, or run
`uv run --package date-planner date-planner-mcp` and see it wait on stdio.

The skill lives in this repo; point Claude Code at it with a symlink, e.g.

```sh
ln -s "$PWD/skills/date-planner" ~/.claude/skills/date-planner
```

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
| `list_past_dates` | Recent dates with ratings, so ideas don't repeat |
| `find_restaurant`, `upsert_restaurant` | The venue record |
| `detect_booking_channel` | Read the venue's website: Resy / OpenTable / Tock / SevenRooms / phone / walk-in / unknown, with evidence and a confidence |
| `build_booking_link` | A prefilled reservation URL for the online platforms |
| `compute_booking_window` | When bookings open for a given date, in the venue's timezone |
| `create_date`, `update_date`, `get_date`, `list_dates` | One date's progress |

A date moves `proposed` → `tentative` → `booking_pending` → `confirmed` →
`completed`, or `cancelled`; `update_date` refuses any other move.

## Booking windows

"One month ahead" is not "30 days ahead". `compute_booking_window` counts
calendar months with `relativedelta`, so a 31 March dinner opens on 28 (or 29)
February, and it combines the open time with the date in the restaurant's own
timezone, so 10:00 local stays 10:00 across a DST change.

## Deep links are verified

The four URL templates were written from the shape of each platform's public
URLs, then checked by hand against live venues on 2026-10-10 — see the note
above `links.VERIFIED` for which venue confirmed each one.
`build_booking_link` reports `verified: true` for all four as of that date.

If a platform changes its URL shape later, `scripts/verify_links.py` prints
fresh links to re-check:

```sh
uv run --package date-planner python scripts/verify_links.py
uv run --package date-planner python scripts/verify_links.py --from-db
```

Open each link. A 404 or "not found" from the platform itself can mean the
sample slug is stale or the venue isn't currently taking reservations there,
rather than the template being wrong — check the platform's own error message
before concluding the URL shape needs a fix. Update the date in
`links.VERIFIED` once you've re-confirmed a template.

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

## Not in v1

Automated booking of any kind (platform APIs, browser automation, phone
calls), multi-user auth, and public hosting. The standalone agent is phase 2:
`SKILL.md` becomes the system prompt, this server is reused as-is, and Google
access moves to OAuth in the app.
