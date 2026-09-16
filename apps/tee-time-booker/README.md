# Tee Time Booker

Books a Soule Park (foreUP) tee time on a schedule using Playwright, and appends
the outcome of every run to `log.csv`.

## How it picks a tee time

Preferences are constants in `src/tee_time_booker/config.py`:

| Setting | Value | Meaning |
| --- | --- | --- |
| `TARGET_WEEKDAY` / `WEEKS_AHEAD` | Saturday / 3 | The 3rd upcoming Saturday (today counts if it is a Saturday), in `America/Los_Angeles` |
| `EARLIEST_TEE_TIME` – `LATEST_TEE_TIME` | 8:00 AM – 2:00 PM | Acceptable tee-off window, inclusive |
| `PREFER_EARLIER` | `True` | Take the earliest slot in the window |
| `HOLES` / `PLAYERS` | 18 / 4 | Booking size |

The run:

1. opens the booking page and logs in
2. queries foreUP's tee-time API through the logged-in browser session and
   chooses a slot per the preferences above
3. sets the date, clicks the slot, picks 18 holes (when the slot offers 9 or
   18) and 4 players, "Book Time", confirms
4. fills the card fields (an OdinPay / Basis Theory hosted iframe) and postal code
5. clicks **Submit** only if `TEE_TIME_SUBMIT_PAYMENT=true`; otherwise it stops
   there (a dry run), saves a screenshot of the filled form to `artifacts/`,
   and logs `Success (dry run)`

On any failure a full-page screenshot is written to `artifacts/`.

Notes on foreUP's behaviour that the code works around:

- It returns **403** to Chromium's default headless user agent, so the browser
  context uses a normal desktop Chrome UA.
- Clicking a slot **holds it for 5 minutes** on your account. When testing
  repeatedly, point `TEE_TIME_TARGET_DATE` at another open date rather than
  waiting.

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
| `TEE_TIME_TARGET_DATE` | no | | Testing aid: book this `YYYY-MM-DD` instead of the computed date |

## Deployment (GitHub Actions)

`.github/workflows/tee-time-booker.yml` runs every Sunday at 08:00 UTC
(1:00 AM PDT / 12:00 AM PST — just after Soule Park releases tee times) and on
manual dispatch. It commits `log.csv` back to the repo after every run and
uploads screenshots as a workflow artifact.

Repository **secrets** needed: `SOULE_EMAIL`, `SOULE_PASSWORD`,
`CREDIT_CARD_NUMBER`, `CREDIT_CARD_EXPIRATION_MONTH`,
`CREDIT_CARD_EXPIRATION_YEAR`, `CREDIT_CARD_CVC`, `CREDIT_CARD_POSTAL_CODE`.

Scheduled runs are dry runs until you set the repository **variable**
`TEE_TIME_SUBMIT_PAYMENT=true`. A manual run can also tick the
"Actually submit payment" box for a one-off real booking.

## Run log

`log.csv` has one row per run:

`run_id, created_at, finished_at, course_name, tee_time_pst, cost_dollars, service_fee_dollars, status`

`cost_dollars` is the green fee reported by the API for the chosen slot;
`service_fee_dollars` is not yet captured. `status` is `Success`,
`Success (dry run)`, or `Error in booking process: <traceback>`.
