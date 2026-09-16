# Tee Time Booker

Books a tee time on a schedule with Playwright, and appends the outcome of
every run to `log.csv`.

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
| `models.py` | `SearchCriteria`, `TeeTime`, `BookingResult` — the vocabulary adapters share |
| `courses.py` | Every bookable course, its platform, and the preferences it's booked with |
| `platforms/base.py` | The adapter contract |
| `platforms/{foreup,golfnow}.py` | One site each |
| `runner.py` | The standard operations: find, book |
| `selection.py` | Which of the available slots wins |
| `dates.py` | Which date to book, and when the site releases it |
| `browser.py` | The Chromium session and the failure artifacts |
| `config.py` | Settings and secrets from the environment |
| `run_log.py` | The CSV row per run |

### Adding a booking site

1. Write `platforms/<site>.py` with a `BookingPlatform` subclass: a `name`, the
   `CourseConfig` subclass it needs, the `PaymentCard` fields its checkout asks
   for, and the four methods.
2. Add the class to `_ADAPTERS` in `platforms/__init__.py`.
3. Add courses to `courses.py` with `platform="<site>"`.

Nothing else in the app needs to know the site exists. Keep the parsing of the
site's own responses in module-level functions (as both adapters do) so they
can be tested without a browser.

### Adding a course

One entry in `courses.py`: its platform, timezone, the identifiers that
platform's adapter needs, the environment variables holding its login, and a
`Preferences` block (which weekday, how far ahead, the acceptable tee-off
window, holes and players). Courses on the same site can use different
accounts.

## How a run picks a tee time

Preferences are per course in `courses.py`. Soule Park's, for example:

| Setting | Value | Meaning |
| --- | --- | --- |
| `target_weekday` / `weeks_ahead` | Saturday / 3 | The 3rd upcoming Saturday (today counts if it is a Saturday), in the course's timezone |
| `earliest` – `latest` | 8:00 AM – 2:00 PM | Acceptable tee-off window, inclusive |
| `prefer_earlier` | `True` | Take the earliest slot in the window |
| `holes` / `players` | 18 / 4 | Booking size |
| `release` | 00:00, from 20:00 | foreUP opens a day's tee sheet at local midnight; a run started at or after 20:00 waits for the coming midnight and counts "today" as the new day |

A run then:

1. waits until 45 s before the release, if the course has one
2. opens the site and signs in
3. searches for the target date, polling for up to 15 min while the day has
   not been released yet
4. takes the best slot in the window; if that fails because someone else got
   it, searches again and tries the next-best, up to 4 times
5. fills in checkout
6. clicks the final submit **only** if `TEE_TIME_SUBMIT_PAYMENT=true`;
   otherwise it stops there (a dry run), saves a screenshot of the filled form
   to `artifacts/`, and logs `Success (dry run)`

On any failure a full-page screenshot and the page HTML are written to
`artifacts/`; the HTML is what you need to repair a selector.

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
- The search endpoint, the login iframe and the detail-page controls were
  confirmed against the live site. The **checkout field ids** come from the
  older Selenium booker and are the most likely thing to need repair; a failed
  run leaves the page HTML in `artifacts/` for exactly that.

## Local development

From the **repository root** (uv workspace):

```bash
uv sync --package tee-time-booker
uv run --package tee-time-booker playwright install chromium
```

Create `apps/tee-time-booker/.env` from `.env.example` and fill in the login
and card details for the site you're booking. Then, from
`apps/tee-time-booker`:

```bash
# book the default course (soule-park)
uv run --package tee-time-booker python -m tee_time_booker

# what's configured, and what's available without booking anything
uv run --package tee-time-booker python -m tee_time_booker --list-courses
uv run --package tee-time-booker python -m tee_time_booker \
    --course saticoy --date 2026-09-19 --find-only
```

`--find-only` needs only the site login, not card details. `--course` and
`--date` override `TEE_TIME_COURSE` and `TEE_TIME_TARGET_DATE`; set
`TEE_TIME_HEADLESS=false` in `.env` to watch the browser.

Tests cover the date logic, slot selection, both adapters' response parsing,
the course/adapter wiring and the CSV log (no browser needed):

```bash
uv run --package tee-time-booker pytest
```

## Environment variables

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `SOULE_EMAIL`, `SOULE_PASSWORD` | for `soule-park` | | foreUP login |
| `GOLFNOW_EMAIL`, `GOLFNOW_PASSWORD` | for `saticoy` | | GolfNow login |
| `CREDIT_CARD_NUMBER`, `CREDIT_CARD_EXPIRATION_MONTH` (MM), `CREDIT_CARD_EXPIRATION_YEAR` (YY), `CREDIT_CARD_CVC`, `CREDIT_CARD_POSTAL_CODE` | to book | | Card entered at checkout |
| `CREDIT_CARD_NAME`, `CREDIT_CARD_BILLING_ADDRESS`, `PHONE_NUMBER` | to book on GolfNow | | Extra fields GolfNow's checkout asks for |
| `TEE_TIME_COURSE` | no | `soule-park` | Course to book |
| `TEE_TIME_SUBMIT_PAYMENT` | no | `false` | Click the final submit button |
| `TEE_TIME_HEADLESS` | no | `true` | Run Chromium headless |
| `TEE_TIME_LOG_PATH` | no | `log.csv` | Run log location |
| `TEE_TIME_ARTIFACTS_DIR` | no | `artifacts` | Screenshot location |
| `TEE_TIME_TARGET_DATE` | no | | Testing aid: book this `YYYY-MM-DD` instead of the computed date, without waiting for a release |

Each adapter declares the card fields its checkout needs, so a foreUP run
never fails over a GolfNow-only variable.

## Deployment (GitHub Actions)

`.github/workflows/tee-time-booker.yml` runs every Sunday at 06:40 UTC
(11:40 PM PDT / 10:40 PM PST Saturday) and on manual dispatch. The booker
itself waits for course-local midnight, so the cron only has to land before
it in both DST states; the job timeout (110 min) covers the longest wait. It
commits `log.csv` back to the repo after every run and uploads screenshots as
a workflow artifact.

The schedule books `TEE_TIME_COURSE` (repository variable; unset means
`soule-park`, whose midnight release the cron is timed for). A manual run can
pick any configured course with the "course" input.

Repository **secrets** needed: `SOULE_EMAIL`, `SOULE_PASSWORD`,
`CREDIT_CARD_NUMBER`, `CREDIT_CARD_EXPIRATION_MONTH`,
`CREDIT_CARD_EXPIRATION_YEAR`, `CREDIT_CARD_CVC`, `CREDIT_CARD_POSTAL_CODE`,
plus `GOLFNOW_EMAIL`, `GOLFNOW_PASSWORD`, `CREDIT_CARD_NAME`,
`CREDIT_CARD_BILLING_ADDRESS` and `PHONE_NUMBER` for GolfNow runs.

Scheduled runs are dry runs until you set the repository **variable**
`TEE_TIME_SUBMIT_PAYMENT=true`. A manual run can also tick the
"Actually submit payment" box for a one-off real booking, and can set
"target_date" to book a specific already-open day.

Suggested rollout for a new course:

1. Manual run, dry run, no date: confirms secrets and the flow in CI.
2. Manual run, **submit payment**, `target_date` = a cheap open weekday: the
   first real charge, on a slot you actually want. Check the
   `confirmation-*.png` artifact and your account; the `Success` log row is
   based on the confirmation page appearing after payment.
3. Set the `TEE_TIME_SUBMIT_PAYMENT=true` variable so scheduled runs book for
   real.

## Run log

`log.csv` has one row per run:

`run_id, created_at, finished_at, course_name, tee_time_pst, cost_dollars, service_fee_dollars, status`

`cost_dollars` is the per-player green fee reported for the chosen slot;
`service_fee_dollars` is GolfNow's booking fee where the site reports one
(foreUP doesn't). `status` is `Success`, `Success (dry run)`, or
`Error in booking process: <traceback>`.
