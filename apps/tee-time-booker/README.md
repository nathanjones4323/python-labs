# Tee Time Booker

Books a tee time on a schedule with Playwright. Who wants what booked, and
what every run did, live in Postgres.

Two booking sites are supported today, behind one interface:

| Platform | Adapter | Courses configured |
| --- | --- | --- |
| foreUP | `platforms/foreup.py` | Soule Park (`soule-park`) |
| GolfNow | `platforms/golfnow.py` | Saticoy Regional (`saticoy`) |

## How it's put together

Booking sites differ in every detail and agree on the shape of the job: find
the day's tee times, take one, pay for it. So an **adapter** implements the
mechanics of one site and nothing else:

```python
class BookingPlatform(ABC):
    def start(criteria) -> None                  # open the site, sign in
    def find_tee_times(criteria) -> list[TeeTime]  # what's bookable that day
    def hold(tee_time, criteria) -> bool         # take this slot, False if it's gone
    def complete_booking(tee_time, criteria, *, submit_payment) -> BookingResult
```

Everything that should behave the same whichever site is being booked lives in
`runner.py`: waiting for a release, polling until the day's sheet opens,
choosing the best slot in the window, moving to the next-best when a hold is
refused, and stopping before payment on a dry run. `runner.find_tee_times()`
and `runner.book_tee_time()` are the app's real interface — the CLI calls them
today and an HTTP layer should call them tomorrow, rather than reaching for an
adapter directly.

| Module | Holds |
| --- | --- |
| `models.py` | `SearchCriteria`, `TeeTime`, `BookingResult`, `Preferences` — the vocabulary adapters share |
| `courses.py` | The shape of a course (`CourseConfig` and its per-platform subclasses) and the seed data |
| `db/models.py` | The tables, and the booking-request state machine |
| `db/service.py` | Claiming a request, recording a run, seeding courses/users/plans |
| `seed.py` | Loads the known courses (and optionally one user's plans) into the database |
| `platforms/base.py` | The adapter contract |
| `platforms/{foreup,golfnow}.py` | One site each |
| `runner.py` | The standard operations: find, book |
| `selection.py` | Which of the available slots wins |
| `dates.py` | Which date to book, and when the site releases it |
| `browser.py` | The Chromium session and the failure artifacts |
| `config.py` | Settings and secrets from the environment |

### Adding a booking site

1. Write `platforms/<site>.py` with a `BookingPlatform` subclass: a `name`, the
   `CourseConfig` subclass it needs, the `PaymentCard` fields its checkout asks
   for, and the four methods.
2. Add the class to `_ADAPTERS` in `platforms/__init__.py`.
3. Add courses to `SEED_COURSES` in `courses.py` with `platform="<site>"` and
   run the seed.

Nothing else in the app needs to know the site exists. Keep the parsing of the
site's own responses in module-level functions (as both adapters do) so they
can be tested without a browser.

### Adding a course

One row in the `courses` table: its platform, timezone, release schedule and
the identifiers that platform's adapter needs (a JSONB blob, so a new site
needs no schema change). The seed in `courses.py` is the easiest way to add
one: append a `CourseConfig` to `SEED_COURSES` and run
`python -m tee_time_booker.seed`, which upserts by key.

## Database

The database is the source of truth for who books what:

| Table | Row |
| --- | --- |
| `users` | Someone with plans |
| `courses` | A bookable course: platform, timezone, release schedule, adapter identifiers |
| `site_accounts` | A user's login on one booking site. The secret stays in the environment: the row names an `env_prefix`, and the login is read from `<PREFIX>_EMAIL` / `<PREFIX>_PASSWORD` |
| `booking_plans` | A standing wish: this course, with this account, the Nth upcoming weekday, this tee-off window, this group size |
| `booking_requests` | A plan pinned to one date (one per plan per date), with the plan's search copied in so editing the plan doesn't change a request in flight |
| `runs` | One attempt at a request: when, what it held, what it cost, how it ended |

A request is the unit of work, and it moves through a state machine that the
model enforces (`BookingRequest.transition`):

```
pending ──▶ running ──▶ booked
   ▲          │  │
   │          │  └────▶ failed ──▶ (running again, or cancelled)
   └──────────┘  └────▶ unknown ──▶ (booked / failed / cancelled, by hand)
   (dry run)
```

A worker claims a request with a row lock (`pending`/`failed` → `running`)
and opens a run; when the run ends, its status decides the request's:
`success` → `booked`, `dry_run` → back to `pending` (nothing was booked),
`error` → `failed`, `unknown` → `unknown` (the final submit was clicked and no
confirmation or error appeared — check the account on the booking site before
touching the request again). A request can't be claimed twice or booked twice,
whatever the caller does.

Card details are not in the database. Every run pays with the one card in the
environment (`CREDIT_CARD_*`); per-user cards need an encryption story first.

Schema changes are Alembic migrations in `migrations/`, generated from the
models:

```bash
# from apps/tee-time-booker, with DATABASE_URL set
uv run --package tee-time-booker alembic upgrade head
uv run --package tee-time-booker alembic revision --autogenerate -m "add foo"
```

## How a run picks a tee time

Preferences are per booking plan. The seeded Soule Park plan, for example:

| Setting | Value | Meaning |
| --- | --- | --- |
| `target_weekday` / `weeks_ahead` | Saturday / 3 | The 3rd upcoming Saturday (today counts if it is a Saturday), in the course's timezone |
| `earliest` – `latest` | 8:00 AM – 2:00 PM | Acceptable tee-off window, inclusive |
| `prefer_earlier` | `True` | Take the earliest slot in the window |
| `holes` / `players` | 18 / 4 | Booking size |
| `release` (on the course) | 00:00, from 20:00 | foreUP opens a day's tee sheet at local midnight; a run started at or after 20:00 waits for the coming midnight and counts "today" as the new day |

A run started with `--plan` works out the plan's target date, finds or creates
the plan's request for that date and claims it; `--request` claims an existing
request directly. It then:

1. refuses a target date the site can't book yet, when the window is known
   up front (foreUP's 20 days); then waits until 45 s before the release, if
   the course has one
2. opens the site and signs in; where the window is only visible on the site
   (GolfNow), measures it now and refuses the target if it's past it
3. searches for the target date, polling for up to 15 min while the day has
   not been released yet
4. takes the best slot in the window; if that fails because someone else got
   it, searches again and tries the next-best, up to 4 times
5. reaches the checkout form. If `TEE_TIME_SUBMIT_PAYMENT` is not `true`
   it stops here (a dry run), saves a screenshot to `artifacts/`, and records
   the run as `dry_run` — card details are never entered on a dry run
6. otherwise fills in the card and clicks the final submit

On any failure a full-page screenshot and the page HTML are written to
`artifacts/`; the HTML is what you need to repair a selector. Card inputs are
hidden in every screenshot, since artifacts are uploaded to GitHub Actions.

## foreUP (Soule Park)

The flow: open the booking page, pick the booking class, log in, query
foreUP's tee-time API through the logged-in browser session, show the date and
click the slot (foreUP grants a 5-minute hold), pick holes and players, "Book
Time", confirm, fill the card iframe, submit.

- **Every step waits on the foreUP API response it triggers**
  (`page.expect_response`) instead of on DOM state or `networkidle`. The
  response also says whether the step worked — e.g. `pending_reservation`
  returns `success: false` when a hold is refused — which the DOM can't.
- **Selectors anchor on hooks foreUP's own JS depends on**: element ids
  (`#login_email`, `#date-field`, `#book_time`), `data-field` / `data-value`
  attributes, and the `autocomplete="cc-*"` attributes inside the card
  iframe. foreUP's modals carry `aria-hidden="true"` even while shown, so
  role-based locators can't reach inside them; outside modals, role/label
  locators are used.
- Slot tiles have no ARIA roles, so they are matched on the rendered time
  label *and* side (Front/Back), since a clock time can appear once per side.
- foreUP returns **403** to Chromium's default headless user agent, so the
  browser context uses a normal desktop Chrome UA.
- Clicking a slot **holds it for 5 minutes** on your account. When testing
  repeatedly, point `--date` at another open date rather than waiting. The
  hold is released when the browser closes.
- Its booking window is **20 days** ahead, released at course-local midnight;
  the 3rd upcoming Saturday from a Sunday is exactly 20 days out, so the
  scheduled run books the Saturday that opens that night.

## GolfNow

The flow: sign in through the GolfID iframe (email, then password on a second
step), ask GolfNow's search API for the day's rates, open the chosen rate's
detail page, pick the number of golfers, "Continue to Book", fill checkout,
Reserve.

- **The search goes to GolfNow's own API**
  (`/api/tee-times/tee-time-search-results`) rather than the results grid,
  which is rendered client-side and restyled often. The response carries the
  rate id, hole count, player rule and prices needed to choose a slot and jump
  straight to its checkout URL.
- The time window and hole count are left wide open in the request and applied
  to the results instead: GolfNow reinterprets those filters server-side, and
  we would rather see every slot than trust its filtering.
- Times come back as `2026-09-19T07:24:00+00:00` but are course-local wall
  times, so the offset is dropped rather than converted.
- One tee time carries several **rates** (9 vs 18 holes, walking vs cart),
  each with its own id, price and `playerRule` (`"Any"`, `"OneTwo"`,
  `"TwoThreeFour"`, ...) saying which group sizes it takes. Each rate that
  fits the criteria becomes its own `TeeTime`.
- GolfNow reserves nothing until payment, so `hold()` means "got as far as
  checkout" — still the step that fails when somebody else took the slot.
- The player-count radios are hidden inputs behind Tailwind labels, so the
  label is clicked; a disabled radio means that group size is gone.
- GolfNow releases availability on a rolling window rather than at a fixed
  local time, so `saticoy` has no `release` schedule and books whatever is
  already open.
- **Each facility sets its own booking window** (Saticoy lists 7 days ahead,
  Sterling Hills three weeks or more) and GolfNow states it nowhere — not on
  the facility page, in its bundles, or in the search response; a day past
  the window is just an empty search. So after signing in the adapter
  measures it, one search per day from today until three empty days in a row
  (capped at 45), logs `takes bookings through <date> (N days ahead)`, and
  the run fails immediately if the target is past that instead of polling
  an empty day for 15 minutes. Use the logged number to set `weeks_ahead`.
- Checkout loads under a **GolfPass upsell interstitial** that hides the whole
  form (every field reports as not visible) until "Skip to checkout" is
  clicked, so the adapter dismisses it before filling anything. A OneTrust
  cookie banner is accepted too so it can't sit over the Reserve button.
- Checkout also requires a **billing state** (`CREDIT_CARD_STATE`, two-letter
  code) alongside the address and postal code; Reserve stays disabled until
  every mandatory field validates, and the adapter waits for it to enable
  before clicking. A dry run doesn't fill the form, so instead it logs which
  of the expected checkout fields are on the page — the first place to look
  when GolfNow renames an id.
- GolfNow auto-opts the booking into a round-up **charitable donation**
  (cents to the next dollar); the adapter leaves that as the site sets it.
- The whole flow up to Reserve — login, search, detail page, checkout — was
  confirmed against the live site with a dry run. What a real Reserve click
  lands on (`CONFIRMATION_URL`) has not been, so the first paid run should be
  watched; a failed run leaves the page HTML in `artifacts/` for repair.

## Local development

From the **repository root** (uv workspace):

```bash
uv sync --package tee-time-booker
uv run --package tee-time-booker playwright install chromium
```

Create `apps/tee-time-booker/.env` from `.env.example` and fill in the login
and card details for the site you're booking. Then, from
`apps/tee-time-booker`, start a Postgres, migrate it and seed it with the
known courses plus your own accounts and plans:

```bash
docker compose up -d --wait
uv run --package tee-time-booker alembic upgrade head
uv run --package tee-time-booker python -m tee_time_booker.seed --user you@example.com
```

The seed prints a plan id per course; put the one you want scheduled in
`TEE_TIME_PLAN`. Then:

```bash
# book the plan in TEE_TIME_PLAN
uv run --package tee-time-booker python -m tee_time_booker

# what's configured, what's happened, and what's available without booking
uv run --package tee-time-booker python -m tee_time_booker --list-courses
uv run --package tee-time-booker python -m tee_time_booker --list-plans
uv run --package tee-time-booker python -m tee_time_booker --runs 10
uv run --package tee-time-booker python -m tee_time_booker \
    --plan <saticoy plan id> --date 2026-09-19 --find-only
```

`--find-only` needs only the site login, not card details. `--plan` and
`--date` override `TEE_TIME_PLAN` and `TEE_TIME_TARGET_DATE`; `--request <id>`
runs one specific request instead. Set `TEE_TIME_HEADLESS=false` in `.env` to
watch the browser.

Tests cover the date logic, slot selection, both adapters' response parsing,
the course/adapter wiring (no browser needed) and, with
`TEE_TIME_TEST_DATABASE_URL` set, the migrations, the state machine and the
CLI's bookkeeping against a real Postgres (the compose file creates
`tee_time_booker_test` for this; it is wiped every run):

```bash
uv run --package tee-time-booker pytest
```

## Environment variables

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `DATABASE_URL` | yes | | Postgres holding courses, plans, requests and runs (`postgresql://...`; the psycopg driver is added automatically) |
| `TEE_TIME_PLAN` | to book | | Booking plan id (`--list-plans`); `--plan` / `--request` override it |
| `SOULE_EMAIL`, `SOULE_PASSWORD` | for the seeded Soule Park account | | foreUP login (`env_prefix` `SOULE`) |
| `GOLFNOW_EMAIL`, `GOLFNOW_PASSWORD` | for the seeded GolfNow account | | GolfNow login (`env_prefix` `GOLFNOW`) |
| `CREDIT_CARD_NUMBER`, `CREDIT_CARD_EXPIRATION_MONTH` (MM), `CREDIT_CARD_EXPIRATION_YEAR` (YY), `CREDIT_CARD_CVC`, `CREDIT_CARD_POSTAL_CODE` | to book | | Card entered at checkout |
| `CREDIT_CARD_NAME`, `CREDIT_CARD_BILLING_ADDRESS`, `CREDIT_CARD_STATE` (two-letter code), `PHONE_NUMBER` | to book on GolfNow | | Extra fields GolfNow's checkout asks for |
| `TEE_TIME_SUBMIT_PAYMENT` | no | `false` | Click the final submit button. An operator kill switch: `false` makes every run a dry run |
| `TEE_TIME_HEADLESS` | no | `true` | Run Chromium headless |
| `TEE_TIME_TEST_DATABASE_URL` | no | | A throwaway database for the test suite; DB tests are skipped without it |
| `TEE_TIME_ARTIFACTS_DIR` | no | `artifacts` | Screenshot location |
| `TEE_TIME_TARGET_DATE` | no | | Testing aid: book this `YYYY-MM-DD` instead of the computed date, without waiting for a release |

Each adapter declares the card fields its checkout needs, so a foreUP run
never fails over a GolfNow-only variable.

## Deployment (GitHub Actions)

`.github/workflows/tee-time-booker.yml` runs every Sunday at 06:40 UTC
(11:40 PM PDT / 10:40 PM PST Saturday) and on manual dispatch. The booker
itself waits for course-local midnight, so the cron only has to land before
it in both DST states; the job timeout (110 min) covers the longest wait. It
runs pending migrations first, records the run in the database, and uploads
screenshots as a workflow artifact.

The schedule books the plan in the `TEE_TIME_PLAN` repository variable, which
should be a Soule Park plan since the cron is timed for its midnight release.
A manual run can pick any plan with the "plan" input. The hosted database
needs the schema applied and the seed run once
(`python -m tee_time_booker.seed --user ...` with `DATABASE_URL` pointing at
it) — that prints the plan ids.

Repository **secrets** needed: `DATABASE_URL`, `SOULE_EMAIL`, `SOULE_PASSWORD`,
`CREDIT_CARD_NUMBER`, `CREDIT_CARD_EXPIRATION_MONTH`,
`CREDIT_CARD_EXPIRATION_YEAR`, `CREDIT_CARD_CVC`, `CREDIT_CARD_POSTAL_CODE`,
plus `GOLFNOW_EMAIL`, `GOLFNOW_PASSWORD`, `CREDIT_CARD_NAME`,
`CREDIT_CARD_BILLING_ADDRESS`, `CREDIT_CARD_STATE` and `PHONE_NUMBER` for
GolfNow runs.

Scheduled runs are dry runs until you set the repository **variable**
`TEE_TIME_SUBMIT_PAYMENT=true`. A manual run can also tick the
"Actually submit payment" box for a one-off real booking, and can set
"target_date" to book a specific already-open day.

Suggested rollout for a new course:

1. Manual run, dry run, no date: confirms secrets and the flow in CI.
2. Manual run, **submit payment**, `target_date` = a cheap open weekday: the
   first real charge, on a slot you actually want. Check the
   `confirmation-*.png` artifact and your account; the `success` run is
   based on the confirmation page appearing after payment.
3. Set the `TEE_TIME_SUBMIT_PAYMENT=true` variable so scheduled runs book for
   real.

## Run history

Every run is a row in `runs` (`python -m tee_time_booker --runs`, or query the
table): when it started and finished, the request it served, the slot it held
(`tee_time_local`, course-local, plus `side`), `green_fee` per player and
`service_fee` where the site reports one (GolfNow does, foreUP doesn't), the
site's `confirmation` after a real booking, and a `status`: `success`,
`dry_run`, `error` (with a short `detail`; the traceback is in the job log)
or `unknown` — the final submit was clicked but no confirmation or error
appeared, so check your account on the booking site before retrying.
