# Campsite Booker

Finds open campsites on recreation.gov and holds them for you. Use it to
book a site that's open now, or to watch a full campground for
cancellations: it checks every minute and messages you on Telegram the moment
a site opens up. Each alert has a **Hold** button that puts the site in your
recreation.gov cart. Or let the watch hold it for you before it even alerts.

A hold is recreation.gov's own cart, so the site is yours for its 15-minute
timer. Pay on recreation.gov (phone or computer, same account), tap **Pay**
in the alert, or have a watch pay by itself. Paying uses the card saved on
your recreation.gov account, so no card number is kept here, and it's a dry
run until you turn it on.

Dates that aren't on sale yet can be watched too. The watch is **armed** for
the release that puts them on sale (Upper Pines: the 15th of the month, 7 AM
Pacific). From just before then it checks every 2 seconds, since popular
parks sell out in under a minute.

Only recreation.gov campgrounds are covered: national parks and forests,
Army Corps lakes and BLM. State parks (ReserveCalifornia) are a different
site.

## How it works

```
recreation.gov JSON API ──every 60 s──▶ watcher ──▶ SQLite (watches, openings, holds)
                                           │
              Telegram ◀── alert + Hold ───┤
                                           └──▶ Playwright: Add to Cart (signed in)
```

| Module | Holds |
| --- | --- |
| `recgov.py` | recreation.gov's public JSON API: search, campground, month availability (no login) |
| `availability.py` | `StayCriteria` and `find_stays`: which sites are free for a whole stay |
| `service.py` | The operations: search what's open, watches, checking watches, recording holds |
| `releases.py` | Release schedules: which watches wait on the next release, and whether it's out |
| `booker.py` | The browser: pick the dates on the site's calendar, Add to Cart, sign in |
| `checkout.py` | Paying: order details, cart, the saved card, Review and Confirm |
| `alerts.py` / `gmail.py` | recreation.gov's own alert emails: reading one, and fetching them from Gmail (read-only) |
| `bot.py` | `campsite-booker`, the always-on watcher and its Telegram bot |
| `mcp.py` | `campsite-booker-mcp`, the same operations as MCP tools for the assistant |
| `cli.py` | `campsite-booker-cli`, the same from a terminal |
| `login.py` | `campsite-booker-login`, a one-time sign-in in a visible browser |
| `gmail_login.py` | `campsite-booker-gmail-login`, a one-time Gmail sign-in for the alert emails |
| `messages.py` | Telegram text |
| `db/` | SQLAlchemy models and engine; migrations in `migrations/` (Alembic) |

**Availability** comes from the JSON the site's own pages load
(`/api/camps/availability/campground/{id}/month`), one request per
campground per month, with no login. Each site has a status per night, and
only `Available` counts. A stay is a site that's `Available` for every
night from check-in to the night before check-out.

**Matching a stay**: the arrival night must be `Available`. The nights
after it can be `Available` or `Open`. Just past the booking window
recreation.gov shows a week of `Open` nights, which can't be booked on their
own, only as the end of a stay that arrives inside the window. The site's
calendar takes such a stay (checked live).

**A watch** is a campground (or some of its sites), an arrival date or range
of arrival dates (optionally only some weekdays), a number of nights, and
optional filters for group size, site type (`tent`, `rv`) and group sites
(off by default). Each check fetches every watched campground once, however
many watches it has. Each matching stay is recorded as an **opening**. You're
alerted when one first appears, and again if it disappears and comes back
(someone's cart expired). Watches end on their own once the last arrival
date passes.

**Auto-hold**: a watch with `auto_hold` puts the first opening it finds
straight into the cart, before sending the alert, since a cancellation is
usually gone within minutes. It does this once per watch. After that the
watch keeps alerting with Hold buttons, so a hold you let lapse isn't grabbed
again every 15 minutes. After a hold you get **Pay on recreation.gov** (the
cart) and **✅ Booked, stop watching** buttons.

**Holding** opens the campsite's page in Chromium, pages its calendar to
the check-in month, clicks check-in and check-out, and presses Add to Cart.
The calendar days are matched on their labels (`Friday, January 1, 2027 -
Available`). A day only gets its status label once its month is showing and
loaded, so a day shown as filler at the end of the previous month is skipped.
Afterwards the page should move to the order details. If an error shows up
instead, the hold is `failed` with the site's words. If neither happens it's
`unknown`: check the cart. A screenshot and the page HTML are saved to
`CAMPSITE_ARTIFACTS_DIR` either way, and the HTML is what you need to fix a
selector.

**Signing in**: recreation.gov's login is behind reCAPTCHA, which a headless
browser tends to fail. So you sign in once, in a visible window
(`campsite-booker-login`). That saves the session (cookies and local storage)
to `CAMPSITE_SESSION_FILE`, mode 600, and every hold reuses it and saves it
again afterwards. If the session has expired, a hold tries the login form
with `RECGOV_EMAIL` / `RECGOV_PASSWORD`. If that's refused, you get a
"sign-in needed" message and nothing is held.

## Paying

Asked to pay (a watch's `auto_pay`, the **💳 Pay** button on a hold, `hold
--pay`, or the assistant's `pay_hold`), the browser carries on from the hold:

1. **Order details**: fills each site's group size (the watch's, or
   `CAMPSITE_GROUP_SIZE`) where the campground asks for it, ticks "I have
   read and agree to this important information", then presses Proceed to
   Cart (`#action-bar-submit`).
2. **Cart**: Proceed to Payment, skipping a park-pass upsell if one shows.
3. **Payment**: the account's **saved card**. recreation.gov's wallet may
   first ask for a saved card's last 4 digits (and expiry) to unlock it.
   That's answered from `CAMPSITE_CARD_*`, once only, because wrong answers
   lock the wallet for 24 hours. Then Next.
4. **Review and Confirm**: stops here unless `CAMPSITE_SUBMIT_PAYMENT=true`
   (a **dry run**: nothing paid, still in the cart). Otherwise it presses
   Confirm and waits for `/cart/confirmation`, reading the order number off
   that page.

Anything it doesn't expect stops the checkout, and the site stays in your
cart with the reason in the message. That includes a field a campground
requires (vehicles, equipment, a permit holder), no saved card, or a wallet
it can't unlock. The hold is the race; paying can wait the 15 minutes.
Paying needs a card saved on the account: this never types a card number.

The steps come from recreation.gov's own JavaScript (the order-details page
and the cart). Nobody has watched them signed in yet, so do one dry run
before turning payment on: `campsite-booker-cli hold <campground>
<site> --arrive ... --pay` with `CAMPSITE_HEADLESS=false`. Every paying run
saves a screenshot (inputs masked) and the page HTML to
`CAMPSITE_ARTIFACTS_DIR`.

A watch only pays with `auto_pay` on (`set_watch_auto_pay`, or `watch
--auto-pay`), **and** with `CAMPSITE_SUBMIT_PAYMENT=true` in `.env`. Either
off means no payment. The assistant asks before turning `auto_pay` on, as it
does before any hold or payment.

## Release days

recreation.gov opens each campground's dates on a schedule. Its releases
API (`/api/camps/campgrounds/{id}/releases`) gives the last arrival date
on sale now, plus when the next release is and how far it reaches. When a
watch's arrival dates fall in the next release, the watcher:

1. tells you once ("⏰ the dates go on sale Sun Nov 15 7:00 AM"), and the
   assistant's `create_watch` returns the same as `on_sale_at`
2. checks the saved sign-in 10 minutes before, and messages you if it's
   signed out, so there's time to run the login
3. from 20 seconds before, checks that campground every
   `CAMPSITE_RELEASE_POLL_SECONDS` (2) and handles what turns up like any
   other check: auto-hold (and pay) first, then the alert
4. stops once the release is out (the first new day no longer shows `Open`
   or `NYR` at any site), or after 10 minutes

A watch whose dates fall in a later release is armed once the release
before it has happened. Schedules are fetched only for campgrounds with a
watch, every 6 hours or after a release. Group sites can have their own
release time, which isn't tracked: a group-site watch is armed for the
individual-site release.

## recreation.gov's alert emails (the fallback)

The availability alerts you set up on recreation.gov are a second trigger.
Every `CAMPSITE_GMAIL_POLL_SECONDS` (30) the watcher looks in Gmail for new
"Campsite Available!" emails from communications@recreation.gov
(read-only, like pay-my-bills). For each one it:

1. reads it: the campground, the site(s) and their links, the alert's dates,
   recreation.gov's own "As of Oct 27, 2026 at 4:03pm (PDT)", and "as many
   as 796 others" also got it
2. checks that campground straight away, if a watch covers it. Anything new
   that check finds is handled like any other find (auto-hold first, then
   the alert, marked "📧 found checking right after recreation.gov's alert
   email")
3. decides who saw the opening first

Each opening records when it opened (`opened_at`; when it came back, for
one that was gone) and what kind of check found it (`found_by`: `poll`,
`release` or `email`). An alert email is matched to the opening for its
site that opened nearest before the email reached Gmail, or failing that,
the first one after. Then:

| Outcome | Meaning |
| --- | --- |
| `watcher_first` | the watcher saw it before the email arrived; `lead_seconds` is by how much |
| `email_first` | the email arrived first, or the email's own check is what found it; `lead_seconds` is negative |
| `missed` | a watch covers the campground but no opening matched within 30 minutes (gone before a check, or your recreation.gov alert's filters are wider than the watch) |
| `not_watched` | no active watch at that campground |

Three places show it:
- **The log** has one line per decided email, `key=value`, so it greps:
  ```
  TIMING outcome=watcher_first gmail_id=… campground=232447 campsites=94 sites=037 recgov_as_of=2026-10-27T23:03:00+00:00 email_received=2026-10-27T23:03:53+00:00 watcher_opened=2026-10-27T23:03:20+00:00 watcher_lead_s=33 email_delay_s=53 email_check_finds=0
  ```
  Plus an `OPENING … found_by=…` line for everything the watcher finds,
  and an `ALERT_EMAIL …` line as each email arrives.
- `campsite-booker-cli timing --days 30` prints a table, one row per email,
  with the counts and medians.
- `/timing` in Telegram, and `alert_timing` from the assistant.

`email_delay_s` is from recreation.gov's "as of" (to the minute, so it reads
up to 59 s long) to the email reaching Gmail. On the first one (Upper Pines
site 037, Oct 27) that was 53 s, with "as many as 796 other individuals" getting it too.

Sign in once to turn it on. It reuses pay-my-bills' Google OAuth client
unless this app has its own, with a separate token:

```bash
cd apps/campsite-booker
uv run --package campsite-booker campsite-booker-gmail-login
```

### Not verified yet

Searching, watching, release schedules, alerting, and everything up to Add
to Cart's login prompt have been run against the live site. The alert
email parser was checked against a real alert. Not run yet: polling Gmail
itself (it needs the Gmail sign-in), and the signed-in part:
- what Add to Cart lands on (assumed: a URL with `/orderdetails`, `/cart` or
  `/reservations/`)
- where an error shows
- the whole checkout above

Watch the first hold (`CAMPSITE_HEADLESS=false`, `campsite-booker-cli hold
... --pay` with payment off). If it comes back `unknown` or stops, the saved
HTML shows what to match.

## Run it

From the repository root (uv workspace):

```bash
uv sync --all-packages            # not --package: see below
uv run --package campsite-booker playwright install chromium
cp apps/campsite-booker/.env.example apps/campsite-booker/.env   # fill it in
cd apps/campsite-booker
uv run --package campsite-booker campsite-booker-login   # once; sign in in the window
uv run --package campsite-booker campsite-booker         # the watcher
```

Use `uv sync --all-packages`, never `uv sync --package campsite-booker`, in
the main checkout. The workspace shares one `.venv`, and a single-package
sync uninstalls every other app's dependencies, including the always-on
pay-my-bills bot's.

The watcher needs its own Telegram bot (@BotFather). Two programs can't long-poll
one bot, and pay-my-bills and the assistant already have theirs. The watcher
only checks while it's running, so run it on a machine that stays on (a
LaunchAgent, like pay-my-bills). Telegram commands: `/watches` (with Stop
buttons), `/holds`, `/check` (now), `/timing` (the watcher vs the alert
emails), `/login` (is the saved session still signed in?).

Run commands from `apps/campsite-booker`, or anywhere: the app reads its
own `.env` whichever directory it starts in.

### From the assistant

`campsite-booker-mcp` is registered in `apps/assistant/.mcp.json`. Ask
"anything open at Upper Pines Nov 6–8?" or "watch Scorpion for any Friday in
November, two nights, hold it if one opens". The tools are
`search_campgrounds`, `find_open_campsites`, `create_watch`, `alert_timing`,
`list_watches`, `stop_watch`, `resume_watch`, `hold_campsite` (with `pay`
to pay too), `pay_hold`, `set_watch_auto_pay` and `list_holds`. The ones
that hold or pay (`hold_campsite`, `pay_hold`, `set_watch_auto_pay`) ask
before running. A watch made from the
assistant is checked by the watcher, which reads the same database.

### From a terminal

```bash
campsite-booker-cli campgrounds "upper pines"
campsite-booker-cli campgrounds --near 34.28,-119.29 --radius 40
campsite-booker-cli open 232447 --arrive 2026-11-06 --nights 2
campsite-booker-cli open 232498 --arrive 2026-11-01 --until 2026-11-30 \
    --nights 2 --days fri,sat --people 4 --type tent
campsite-booker-cli watch 232447 --arrive 2026-11-06 --nights 2 --auto-hold
campsite-booker-cli watches
campsite-booker-cli stop 3
campsite-booker-cli hold 232447 100 --arrive 2026-11-06 --nights 2
campsite-booker-cli hold 232447 100 --arrive 2026-11-06 --nights 2 --pay --people 4
campsite-booker-cli pay 7          # an earlier hold
campsite-booker-cli watch 232447 --arrive 2027-03-19 --nights 2 --auto-pay
campsite-booker-cli timing --days 30   # the watcher vs the alert emails
```

(each prefixed with `uv run --package campsite-booker`)

## Development

```bash
uv run --package campsite-booker pytest apps/campsite-booker
uv run ruff check apps/campsite-booker

# After changing db/models.py:
cd apps/campsite-booker
uv run --package campsite-booker alembic revision --autogenerate -m "what changed"
```

Tests use no network or browser. recreation.gov responses are real ones
trimmed into `tests/fixtures` (Santa Cruz Island Scorpion, November 2026),
and the database is a throwaway SQLite file migrated with Alembic. Telegram
and the browser are fakes. Be gentle with recreation.gov, a public service:
`CAMPSITE_POLL_SECONDS` has a 30 s floor.

## Environment variables

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `RECGOV_EMAIL`, `RECGOV_PASSWORD` | to sign in | | recreation.gov login |
| `TELEGRAM_BOT_TOKEN` | for the watcher | | Its own bot, from @BotFather |
| `TELEGRAM_ALLOWED_USER_IDS` | for the watcher | | Who it answers and alerts, comma-separated |
| `CAMPSITE_POLL_SECONDS` | no | `60` | Seconds between checks (min 30) |
| `CAMPSITE_RELEASE_POLL_SECONDS` | no | `2` | Seconds between checks while dates go on sale (min 1) |
| `CAMPSITE_SUBMIT_PAYMENT` | no | `false` | Press the final Confirm. Off, every checkout is a dry run |
| `CAMPSITE_CARD_LAST4`, `CAMPSITE_CARD_EXP_MONTH` (MM), `CAMPSITE_CARD_EXP_YEAR` (YY) | if the wallet asks | | Unlocks recreation.gov's wallet for the saved card |
| `CAMPSITE_GROUP_SIZE` | no | `2` | Group size on the order when a watch has none |
| `CAMPSITE_GMAIL_POLL_SECONDS` | no | `30` | Seconds between looks for recreation.gov's alert emails (min 10; 0 is off) |
| `CAMPSITE_GMAIL_CLIENT_SECRETS` | no | this app's, else pay-my-bills' | Desktop OAuth client for the Gmail sign-in |
| `CAMPSITE_GMAIL_TOKEN_FILE` | no | `~/.config/campsite-booker/gmail-token.json` | The saved Gmail sign-in |
| `CAMPSITE_HEADLESS` | no | `true` | `false` to watch holds happen |
| `CAMPSITE_TIMEZONE` | no | `America/Los_Angeles` | What "today" is for watches |
| `CAMPSITE_DATABASE_URL` | no | `~/.local/share/campsite-booker/campsites.db` | SQLite (or Postgres) URL |
| `CAMPSITE_SESSION_FILE` | no | `~/.local/share/campsite-booker/session.json` | The saved sign-in |
| `CAMPSITE_ARTIFACTS_DIR` | no | `~/.local/share/campsite-booker/artifacts` | Screenshots and HTML from holds that didn't work |
