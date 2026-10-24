# Tee Time Booker

Books a tee time with Playwright — from the command line, over HTTP (and so
as MCP tools), or by texting or talking to a Telegram bot, either right away
or scheduled to book itself the moment a day opens.
Who wants what booked, and what every run did, live in Postgres, or in a
SQLite file for someone running it on their own machine.

Two booking sites are supported today, behind one interface:

| Platform | Adapter | Courses configured |
| --- | --- | --- |
| foreUP | `platforms/foreup.py` | Soule Park (`soule-park`) |
| GolfNow | `platforms/golfnow.py` | Saticoy Regional (`saticoy`), Olivas Links (`olivas-links`), Buenaventura (`buenaventura`), River Ridge Vineyard (`river-ridge-vineyard`), River Ridge Victoria Lakes (`river-ridge-victoria-lakes`), Sterling Hills (`sterling-hills`), Ojai Valley Inn (`ojai-valley-inn`), Camarillo Springs (`camarillo-springs`), Los Robles Greens (`los-robles-greens`), Tierra Rejada (`tierra-rejada`), Lake Lindero (`lake-lindero`), Santa Barbara Golf Club (`santa-barbara-gc`), Glen Annie (`glen-annie`), and the 55 other GolfNow facilities within 75 miles of Ventura (Los Angeles, the San Fernando Valley, Pasadena, the South Bay, Long Beach, Santa Clarita, the Antelope Valley and Solvang; keys in `courses.py`) |

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
and `runner.book_tee_time()` are the app's real interface; `worker.py` wraps
the latter with the run bookkeeping (drive the site, record the outcome, move
the request on), and both the CLI and the HTTP API go through it rather than
reaching for an adapter directly.

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
| `worker.py` | Carrying out a claimed request and recording the run; `BookingWorker` does it on a background thread for the API |
| `scheduler.py` | Starting auto-booked plans and requests as soon as their day can be booked (see "Scheduled booking") |
| `main.py` | The CLI |
| `api/` | The FastAPI app: the same operations as routes (see "HTTP API") |
| `mcp.py` | The API's routes as MCP tools, via FastMCP |
| `bot/` | The Telegram bot: a model (Claude, a local one, or a free hosted one; `llm.py`, `local_model.py`) over the MCP tools, the payment confirmation, optional voice notes, run updates (see "Chat and voice") |
| `setup.py` | First-time setup for running it yourself: `.env`, the SQLite database, your plans |
| `selection.py` | Which of the available slots wins |
| `dates.py` | Which date to book, and when the site releases it |
| `browser.py` | The Chromium session and the failure artifacts |
| `config.py` | Settings and secrets from the environment |

### Adding a booking site

1. Write `platforms/<site>.py` with a `BookingPlatform` subclass: a `name`, the
   `CourseConfig` subclass it needs, the `PaymentDetails` fields its checkout asks
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

A course can also carry a short `description` and a `GreenFees` estimate
(weekday and weekend low/high for a 9- or 18-hole round, and the date the
site's listings were sampled). Both come back on `GET /courses` so a client
can pick a course without opening the site. The GolfNow seeds have both,
read from each facility's GolfNow page and two weeks of its listed rates;
they're estimates to set expectations with, not quotes, so refresh them
(and `SAMPLED_ON` / `WIDER_SAMPLED_ON`) when they drift.

## Database

The database is the source of truth for who books what. It's Postgres for the
hosted install and CI, or a SQLite file (`DATABASE_URL=sqlite:///...`) for
someone running the booker on their own machine; the models and migrations
work on both. On SQLite every transaction takes the write lock up front
(`BEGIN IMMEDIATE`), standing in for Postgres's row lock when a request is
claimed:

| Table | Row |
| --- | --- |
| `users` | Someone with plans |
| `courses` | A bookable course: platform, timezone, release schedule, adapter identifiers, and optionally a description, green-fee estimate, review summary and USGA tee rating |
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

Card details are not in the database. A GolfNow run pays with the card saved on
the account it signs in to; a foreUP run types the one card in the environment
(`CREDIT_CARD_*`). Per-user cards need an encryption story first.

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
   the run as `dry_run` — nothing is filled in on a dry run
6. otherwise fills in the payment form and clicks the final submit

On any failure a full-page screenshot and the page HTML are written to
`artifacts/`; the HTML is what you need to repair a selector. Card and personal inputs are
hidden in every screenshot, in case artifacts are ever shared.

## HTTP API

Everything the CLI does is also a route, so a booking can be started or
scheduled by a script, a UI, or an agent:

```bash
# from apps/tee-time-booker, with .env filled in
uv run --package tee-time-booker uvicorn --factory tee_time_booker.api:create_app --port 8000
open http://localhost:8000/docs
```

| Route | Does |
| --- | --- |
| `GET /courses`, `GET /courses/{key}` | The bookable courses, each with its `miles_from_home` (from Ventura) |
| `GET /tee-times?user_email=&date=&players=&holes=&earliest=&latest=&courses=&within_miles=` | What's open on a day in a tee-off window across the courses within `within_miles` of Ventura (default `DEFAULT_SEARCH_MILES`, 40: Ventura County, Ojai, the Conejo Valley and Santa Barbara; up to `MAX_SEARCH_MILES`, 75, reaches Los Angeles), or just `courses` wherever they are, no plan needed. One sign-in per site, the sites in parallel, so all courses take about as long as one |
| `GET /plans?user_email=`, `GET /plans/{id}` | Booking plans, each with the `next_target_date` it would book now (and `first_attempt_at` when auto-booked) |
| `POST /plans` | Create a plan, or update the user's plan of that name (the user must exist — see the seed). `auto_book: true` schedules it |
| `POST /plans/{id}/auto-book` | Turn a plan's automatic booking on or off |
| `DELETE /plans/{id}` | Delete a plan; its requests and runs stay |
| `GET /plans/{id}/tee-times?date=` | Sign in and list what's bookable for the plan, without booking. Slow (a real browser); no card details needed |
| `POST /plans/{id}/book` | Find or create the plan's request for its next date (or `target_date`), claim it and start a run → **202** with a `run_id` |
| `POST /requests` | A one-off request: one date at one course, no plan behind it. `auto_book: true` schedules it |
| `GET /requests?status=&user_email=`, `GET /requests/{id}`, `GET /requests/{id}/runs` | Requests and their attempts |
| `POST /requests/{id}/run` | Start a run on a pending or failed request → **202** |
| `POST /requests/{id}/cancel` | Cancel a request that isn't running or booked |
| `GET /runs?limit=`, `GET /runs/{id}` | Run history; poll `GET /runs/{id}` after a 202 |

A booking is a **job, not a request**: it may sleep up to 95 min for a
release and then drives a browser for minutes, so the booking routes claim
the request in the database (an already running or booked request is a
**409**, straight away), hand the run to a small thread pool and answer at
once. The run's row is the record of what's happening; poll it until `status`
leaves `running`. The service layer's errors map to one status each: unknown
id → 404, a move the request's state machine forbids → 409, a bad date or
mismatched account → 422, the booking site or a missing secret → 502.

Plans and requests name the site account by `env_prefix`; leave it out and
the user's only account on the course's site is used.

Paying needs **both** switches: the operator's `TEE_TIME_SUBMIT_PAYMENT` in
the environment and `submit_payment: true` in the request body. Either off is
a dry run, and the 202 says which (`dry_run`). Passing a `target_date` means
"an already-open day" and turns `wait_for_release` off unless you say
otherwise, like `--date` on the CLI.

Two things to know before running it somewhere:

- **There is no authentication.** Run it where only you can reach it.
- **Runs live in the API process.** If it is killed mid-run the request stays
  `running` until you fix it by hand; a run still queued at shutdown is
  recorded as an error so its request can be retried. Stopping uvicorn waits
  for in-flight runs.

### MCP

`tee_time_booker.mcp` turns the API into an MCP server with FastMCP's
`from_fastapi`: it reads the OpenAPI schema and calls the routes in-process,
so each route's `operation_id` is a tool name (`list_plans`, `find_tee_times`,
`book_plan`, `get_run`, ...) and its docs are the tool description. Nothing
in `mcp.py` knows what the tools do; change the API and the tools follow.

```bash
uv sync --package tee-time-booker --extra mcp
uv run --package tee-time-booker python -m tee_time_booker.mcp          # stdio
uv run --package tee-time-booker python -m tee_time_booker.mcp --http   # http://127.0.0.1:8001/mcp
```

For Claude Desktop / Claude Code (stdio), point the server command at that
module with `DATABASE_URL` and the site logins in its environment; the
agent then books with `book_plan` and polls `get_run`. The API's
`/health` route is left out of the tools.

An MCP client can schedule bookings too (`create_plan` / `create_request`
with `auto_book`), but the scheduler only runs inside the Telegram bot, so
those book only while the bot is running against the same database.

## Chat and voice (Telegram)

`python -m tee_time_booker.bot` books from your phone: text the bot "what's
open at Saticoy Saturday morning?" or "book my Soule plan, 4 players after
9".

```bash
uv sync --package tee-time-booker --extra bot
uv run --package tee-time-booker python -m tee_time_booker.bot
```

To talk instead of type, use your phone keyboard's dictation (the mic key)
and send the text. It needs no setup, and you see the words, and can fix a
misheard course or time, before you send. Telegram's own "transcribe" button
on voice messages doesn't help here: bots receive only the audio, never that
transcript. To send voice notes anyway, add the `voice` extra
(`--extra voice`) and the bot transcribes them itself (see below).

It's one process holding the API, its booking worker and the MCP tools, with
a model (`bot/agent.py`) reading each message and calling the tools:

- **Your choice of model** (`bot/llm.py`): Claude; a model on this machine
  through [Ollama](https://ollama.com), free and private (`setup` picks the
  strongest tool-calling model the memory fits, from `bot/local_model.py`,
  downloads it and gives it a context window the tools fit in); or any
  OpenAI-compatible API, such as a free OpenRouter, Gemini or OpenCode Zen
  model. The agent speaks the Messages API; the OpenAI-compatible model
  translates to chat completions and back. Claude follows the booking rules
  best; smaller local models can misread dates or pick odd tools, but the
  payment gate below is code, so no model pays without your tap.

- **Long polling**: the bot asks Telegram for messages, so the machine opens
  no ports and needs no public address.
- **Only you**: every update from a Telegram user not in
  `TELEGRAM_ALLOWED_USER_IDS` is dropped before any handler runs.
- **Booking tools only**: the model gets an allowlist of the MCP tools
  (`bot/tools.py`): looking (every course at once unless you name some),
  booking, scheduling, cancelling, plans, run history. No shell, no files.
- **One card of tee times**: the model picks the best slots from a search, up to
  twelve across courses, and the bot shows them as one card with a Book
  button each, taken only from what the search actually returned.
- **Paying needs your tap**: a call that pays is not run — booking now
  (`book_plan` / `run_request` with `submit_payment: true`, or a Book button
  on a tee-time card) or scheduling a booking to pay later (`create_plan` /
  `create_request` with `auto_book: true`, `set_plan_auto_book` on). The bot
  sends a Confirm / Cancel button describing what will be booked, and runs
  the call only when you tap Confirm (within 15 minutes; each button works
  once). For a scheduled booking that tap is the only one: it books and pays
  later without asking again. Dry runs, pausing and cancelling go ahead
  without asking, and with `TEE_TIME_SUBMIT_PAYMENT` off every run is a dry
  run, so there's nothing to confirm.
- **Scheduled booking**: the bot runs the scheduler every minute (see
  "Scheduled booking") and sends each scheduled run's outcome to every
  allowed user. A try that finds the day not open yet isn't reported.
- **Run updates**: a started run is watched (every 30 s) and its outcome sent
  to the chat: booked, dry run, failed, or the "payment sent, no confirmation
  seen" case. The watch is in memory, so a run still going when the bot stops
  isn't reported after a restart; `/runs` lists recent runs. Stopping the
  bot waits for runs in progress, as stopping the API does.
- **Voice notes (optional) are transcribed on this machine** with
  faster-whisper (`bot/voice.py`; the model downloads on first use), and the
  bot replies with what it heard before acting on it. Without the `voice`
  extra, the bot answers a voice note with the command to install it.

Commands: `/help`, `/new` (forget the conversation), `/runs`. Each chat's
history is kept in memory until `/new` or a restart.

Telegram bot chats aren't end-to-end encrypted, so nothing secret goes
through the chat: logins and the card stay in `.env` and are only ever read
by the booker itself.

## Run it yourself

Someone else can run their own copy, with their own logins and card, on
their own machine: nothing is shared with you or anyone else. They need
[uv](https://docs.astral.sh/uv/), a Telegram account, and accounts on the booking sites they use. The repository is private, so
they also need read access to it (a collaborator invite).

```bash
git clone https://github.com/nathanjones4323/python-labs.git
cd python-labs
uv sync --package tee-time-booker --extra bot  # add --extra voice for voice notes
uv run --package tee-time-booker playwright install chromium
cd apps/tee-time-booker
uv run --package tee-time-booker python -m tee_time_booker.setup
uv run --package tee-time-booker python -m tee_time_booker.bot
```

On Windows, install [Git](https://git-scm.com/download/win) and uv first
(`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`,
then reopen PowerShell); the same commands work there, with
`cd apps\tee-time-booker`.

`setup` walks through making the bot with @BotFather, finding your Telegram
user id, choosing the model (a local one by default: it checks the machine's
memory, and needs [Ollama](https://ollama.com/download) installed and
running; or a free hosted API, or Claude with an Anthropic key), and the logins (and, for foreUP, the card) for the courses you book.
It writes them to `.env` (mode 600), creates a SQLite database next to it
and loads the courses with a default plan for each course you chose.
Rerunning it keeps what's already in `.env` and asks only for what's missing;
`setup --model` asks only for the model again, to switch. Whichever model you
pick gets one test message first, so a bad key, or a free model that's
rate-limited or refuses this kind of app, shows up during setup with the
provider's reason, and you can pick another.

Things to know:

- **The machine has to be on.** Bookings run on it, so a laptop that's asleep
  at a course's release (midnight for Soule Park) misses it. Booking an
  already-open day from the chat works whenever the bot is running.
- **Dry runs first.** `TEE_TIME_SUBMIT_PAYMENT=false` until you've seen a dry
  run reach checkout; then set it to `true` in `.env` and restart the bot.
- **Only the courses in `courses.py`** can be booked; adding one is described
  under "Adding a course".

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
- **Payment is the card saved on the GolfNow account**, so a GolfNow run
  never holds a card number. With a default payment method saved, checkout
  renders a "Saved Cards" dropdown with it selected (`#selCreditCard`,
  `UseSavedCreditCard=true`) and collapses the new-card form; the adapter
  checks that's the case — on dry runs too — and stops if there's no saved
  card, if it has expired, or if GolfNow wants its CVV re-entered
  (`data-iscvvrequired`), rather than fall back to typing a card.
- Even with a saved card, checkout wants a **billing state and postal code**
  (`CREDIT_CARD_STATE`, two-letter code, and `CREDIT_CARD_POSTAL_CODE`): the
  profile fills in name, address and phone but never those two. The adapter
  fills them only when empty and ticks "Save billing address to your account
  profile" so GolfNow keeps them. Reserve stays disabled until every
  mandatory field validates, and the adapter waits for it to enable before
  clicking. A dry run doesn't fill the form, so instead it logs which of the
  expected checkout fields are on the page — the first place to look when
  GolfNow renames an id.
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
and payment details for the site you're booking (or run the setup wizard,
see "Run it yourself"). Then, from `apps/tee-time-booker`, migrate the
database and seed it with the known courses plus your own accounts and plans.
`.env.example` points at a SQLite file; for Postgres, start the compose one
and point `DATABASE_URL` at it first:

```bash
docker compose up -d --wait   # only for Postgres
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
watch the browser. The same operations over HTTP: see "HTTP API" above.

Tests cover the date logic, slot selection, both adapters' response parsing,
the course/adapter wiring (no browser needed), the bot's payment gate and
tool allowlist (no Telegram or model needed), and the migrations, the state
machine, and the CLI's and the API's bookkeeping against a real database: a
throwaway SQLite file by default, or the Postgres in
`TEE_TIME_TEST_DATABASE_URL` (the compose file creates `tee_time_booker_test`
for this; it is wiped every run). Run both when touching the models or the
migrations:

```bash
uv run --package tee-time-booker pytest
TEE_TIME_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5433/tee_time_booker_test \
    uv run --package tee-time-booker pytest
```

The bot's tests skip themselves unless the `bot` extra is installed.

## Environment variables

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `DATABASE_URL` | yes | | The database holding courses, plans, requests and runs: Postgres (`postgresql://...`; the psycopg driver is added automatically) or SQLite (`sqlite:////absolute/path.db`) |
| `TEE_TIME_PLAN` | to book | | Booking plan id (`--list-plans`); `--plan` / `--request` override it |
| `SOULE_EMAIL`, `SOULE_PASSWORD` | for the seeded Soule Park account | | foreUP login (`env_prefix` `SOULE`) |
| `GOLFNOW_EMAIL`, `GOLFNOW_PASSWORD` | for the seeded GolfNow account | | GolfNow login (`env_prefix` `GOLFNOW`) |
| `CREDIT_CARD_NUMBER`, `CREDIT_CARD_EXPIRATION_MONTH` (MM), `CREDIT_CARD_EXPIRATION_YEAR` (YY), `CREDIT_CARD_CVC`, `CREDIT_CARD_POSTAL_CODE` | to book on foreUP | | Card typed into Soule Park's checkout; it has no card on file |
| `CREDIT_CARD_STATE` (two-letter code), `CREDIT_CARD_POSTAL_CODE` | to book on GolfNow | | Billing details GolfNow asks for alongside the card saved on the account |
| `TEE_TIME_SUBMIT_PAYMENT` | no | `false` | Click the final submit button. An operator kill switch: `false` makes every run a dry run |
| `TEE_TIME_HEADLESS` | no | `true` | Run Chromium headless |
| `TEE_TIME_TEST_DATABASE_URL` | no | | A throwaway Postgres for the test suite; without it DB tests use a temporary SQLite file |
| `TELEGRAM_BOT_TOKEN` | for the bot | | From @BotFather |
| `TELEGRAM_ALLOWED_USER_IDS` | for the bot | | Numeric Telegram user ids the bot answers, comma-separated |
| `TEE_TIME_USER_EMAIL` | for the bot | | The `users` row the bot books as |
| `TEE_TIME_BOT_PROVIDER` | for the bot | `anthropic` if `ANTHROPIC_API_KEY` is set | Who runs the model: `anthropic`, `local` (Ollama) or `openai` (any OpenAI-compatible API) |
| `ANTHROPIC_API_KEY` | for `anthropic` | | Claude's key |
| `TEE_TIME_BOT_BASE_URL`, `TEE_TIME_BOT_API_KEY` | for `openai` | | The API's base URL and key |
| `TEE_TIME_BOT_MODEL` | for `openai` | `claude-sonnet-5-5` / `tee-time-booker` (local) | The model |
| `TEE_TIME_BOT_EFFORT` | no | `medium` | Claude's effort level (`low` … `max`; `anthropic` only) |
| `TEE_TIME_WHISPER_MODEL` | no | `base.en` | The local Whisper model for voice notes (`voice` extra) |
| `TEE_TIME_ARTIFACTS_DIR` | no | `artifacts` | Screenshot location |
| `TEE_TIME_TARGET_DATE` | no | | Testing aid: book this `YYYY-MM-DD` instead of the computed date, without waiting for a release |

Each adapter declares the payment fields its checkout needs, so a foreUP run
never fails over a GolfNow-only variable, and a GolfNow run never needs a card number.

## Scheduled booking

An **auto-booked** plan or one-off request (`auto_book`) is booked by the
scheduler (`scheduler.py`) as soon as its day can be booked, and pays without
asking again: confirming it once, when you schedule it, is the permission.
The Telegram bot runs the scheduler every minute, so scheduled bookings
happen only while the bot is running, on a machine that's awake. When a
request is due depends on its course:

- **A fixed release** (Soule Park: midnight, 20 days ahead): the run starts
  10 minutes before the release that opens the day and waits for it, or at
  once if the day is already open. A plan's next date moves on at
  `pre_release_start` (8 PM the evening before), so an armed weekly plan
  books every week right at the drop.
- **A rolling window** (GolfNow, where each facility's window is only known
  signed in): one try a morning at 6 AM course-local, from 30 days before
  the day. A try that finds the day still beyond the window is a `not_open`
  run and leaves the request pending for the next morning.

Each request gets one real scheduled attempt: once a scheduled run has done
anything but find the day not open (booked, failed, dry run), the scheduler
leaves it alone, so a failure is reported, not retried in a loop. It never
tries twice in one course-local day, and never a date that has passed. A
manual run (a dry run to test, say) doesn't use up the scheduled attempt.

Pause a plan with `set_plan_auto_book` (or `POST /plans/{id}/auto-book`);
pausing it also stops its waiting request. Updating a plan turns auto-booking
off unless the update asks for it again. Call off a scheduled one-off with
`cancel_request`.

With `TEE_TIME_SUBMIT_PAYMENT` off, scheduled runs are dry runs like any
other.

## Run history

Every run is a row in `runs` (`python -m tee_time_booker --runs`, or query the
table): when it started and finished, the request it served, the slot it held
(`tee_time_local`, course-local, plus `side`), `green_fee` per player and
`service_fee` where the site reports one (GolfNow does, foreUP doesn't), the
site's `confirmation` after a real booking, whether the scheduler started it
(`scheduled`), and a `status`: `success`, `dry_run`, `error` (with a short
`detail`; the traceback is in the log), `not_open` (the day wasn't in the
site's booking window yet; the request stays pending) or `unknown` — the
final submit was clicked but no confirmation or error appeared, so check your
account on the booking site before retrying.
