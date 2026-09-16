# Tee Time Booker

Books a Soule Park (foreUP) tee time on a schedule using Playwright, and appends
the outcome of every run to `log.csv`.

## How it picks a tee time

Preferences are constants in `src/tee_time_booker/config.py`:

| Setting | Value | Meaning |
| --- | --- | --- |
| `TARGET_WEEKDAY` / `WEEKS_AHEAD` | Saturday / 3 | The 3rd upcoming Saturday (today counts if it is a Saturday), in `America/Los_Angeles` |
| `RELEASE_TIME` / `PRE_RELEASE_START` | 00:00 / 20:00 | foreUP opens a day's tee sheet at local midnight, 20 days out. A run started at or after 20:00 waits for the coming midnight and counts "today" as the new day |
| `BOOKING_CLASS_NAME` | Regular | Label of the booking-class button on the landing page |
| `EARLIEST_TEE_TIME` – `LATEST_TEE_TIME` | 8:00 AM – 2:00 PM | Acceptable tee-off window, inclusive |
| `PREFER_EARLIER` | `True` | Take the earliest slot in the window |
| `HOLES` / `PLAYERS` | 18 / 4 | Booking size |

The run:

1. if started in the evening, sleeps until 45 s before midnight
2. opens the booking page, picks the booking class and logs in
3. queries foreUP's tee-time API through the logged-in browser session
   (polling for up to 15 min while the day is not released yet) and chooses a
   slot per the preferences above
4. shows the date and clicks the slot; foreUP grants a 5-minute hold. If the
   hold is refused (someone else got it first) it re-queries and tries the
   next-best slot, up to 4 times
5. picks 18 holes (when the slot offers 9 or 18) and 4 players, "Book Time",
   confirms
6. fills the card fields (an OdinPay / Basis Theory hosted iframe) and postal code
7. clicks **Submit** only if `TEE_TIME_SUBMIT_PAYMENT=true`; otherwise it stops
   there (a dry run), saves a screenshot of the filled form to `artifacts/`,
   and logs `Success (dry run)`

On any failure a full-page screenshot and the page HTML are written to
`artifacts/`; the HTML is what you need to repair a selector.

### How the Playwright code stays robust

- **Every step waits on the foreUP API response it triggers**
  (`page.expect_response`) instead of on DOM state or `networkidle`. The
  response also says whether the step worked — e.g. `pending_reservation`
  returns `success: false` when a hold is refused — which the DOM can't.
- **Selectors anchor on hooks foreUP's own JS depends on**: element ids
  (`#login_email`, `#date-field`, `#book_time`), `data-field` / `data-value`
  attributes, and the `autocomplete="cc-*"` attributes inside the card
  iframe. Positional (`nth-child`) and Bootstrap-utility-class selectors are
  gone. Outside modals, role/label locators are used (`Log In` link, the
  booking-class button).
- foreUP's modals carry `aria-hidden="true"` even while shown, so role-based
  locators can't reach inside them — hence ids and `data-*` there.
- Slot tiles have no ARIA roles; they are matched on the rendered time label
  *and* side (Front/Back), since a clock time can appear once per side.

Notes on foreUP's behaviour that the code works around:

- It returns **403** to Chromium's default headless user agent, so the browser
  context uses a normal desktop Chrome UA.
- Clicking a slot **holds it for 5 minutes** on your account. When testing
  repeatedly, point `TEE_TIME_TARGET_DATE` at another open date rather than
  waiting. The hold is released when the browser closes, so back-to-back dry
  runs against the same date simply take the next slot.
- Its booking window is **20 days** ahead, released at course-local midnight;
  the 3rd upcoming Saturday from a Sunday is exactly 20 days out, so the
  scheduled run books the Saturday that opens that night.

## Local development

From the **repository root** (uv workspace):

```bash
uv sync --package tee-time-booker
uv run --package tee-time-booker playwright install chromium
```

Create `apps/tee-time-booker/.env` from `.env.example` and fill in the Soule
Park login and card details. Then, from `apps/tee-time-booker`:

```bash
uv run --package tee-time-booker python -m tee_time_booker
```

Set `TEE_TIME_HEADLESS=false` in `.env` to watch the browser.

Tests cover the date and slot-selection logic and the CSV log (no browser needed):

```bash
uv run --package tee-time-booker pytest
```

## Environment variables

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `SOULE_EMAIL`, `SOULE_PASSWORD` | yes | | foreUP login |
| `CREDIT_CARD_NUMBER`, `CREDIT_CARD_EXPIRATION_MONTH` (MM), `CREDIT_CARD_EXPIRATION_YEAR` (YY), `CREDIT_CARD_CVC`, `CREDIT_CARD_POSTAL_CODE` | yes | | Card entered at checkout |
| `TEE_TIME_SUBMIT_PAYMENT` | no | `false` | Click the final Submit button |
| `TEE_TIME_HEADLESS` | no | `true` | Run Chromium headless |
| `TEE_TIME_LOG_PATH` | no | `log.csv` | Run log location |
| `TEE_TIME_ARTIFACTS_DIR` | no | `artifacts` | Screenshot location |
| `TEE_TIME_TARGET_DATE` | no | | Testing aid: book this `YYYY-MM-DD` instead of the computed date, without waiting for a release |

## Deployment (GitHub Actions)

`.github/workflows/tee-time-booker.yml` runs every Sunday at 06:40 UTC
(11:40 PM PDT / 10:40 PM PST Saturday) and on manual dispatch. The booker
itself waits for course-local midnight, so the cron only has to land before
it in both DST states; the job timeout (110 min) covers the longest wait. It
commits `log.csv` back to the repo after every run and uploads screenshots as
a workflow artifact.

Repository **secrets** needed: `SOULE_EMAIL`, `SOULE_PASSWORD`,
`CREDIT_CARD_NUMBER`, `CREDIT_CARD_EXPIRATION_MONTH`,
`CREDIT_CARD_EXPIRATION_YEAR`, `CREDIT_CARD_CVC`, `CREDIT_CARD_POSTAL_CODE`.

Scheduled runs are dry runs until you set the repository **variable**
`TEE_TIME_SUBMIT_PAYMENT=true`. A manual run can also tick the
"Actually submit payment" box for a one-off real booking, and can set
"target_date" to book a specific already-open day.

Suggested rollout:

1. Manual run, dry run, no date: confirms secrets and the flow in CI.
2. Manual run, **submit payment**, `target_date` = a cheap open weekday: the
   first real charge, on a slot you actually want. Check the
   `confirmation-*.png` artifact and your foreUP account; the `Success`
   log row is based on the confirmation page appearing after payment.
3. Set the `TEE_TIME_SUBMIT_PAYMENT=true` variable so Saturday-night runs
   book for real.

## Run log

`log.csv` has one row per run:

`run_id, created_at, finished_at, course_name, tee_time_pst, cost_dollars, service_fee_dollars, status`

`cost_dollars` is the green fee reported by the API for the chosen slot;
`service_fee_dollars` is not yet captured. `status` is `Success`,
`Success (dry run)`, or `Error in booking process: <traceback>`.
