# Pay My Bills

Ventura Water and SCE are both on autopay. When either one takes the money,
this app works out the roommate's share and sends you a Telegram message with
a **Request on Venmo** button. The amount, recipient and note are already
filled in, so you tap the button and then Request.

There's no browser automation, and the app never moves money itself. It only
reads Gmail.

## How it works

```
Gmail (read-only) ──poll every 2 min──▶ parsers ──▶ pipeline ──▶ SQLite
                                                        │
                       Telegram ◀──── outbox ◀──────────┘
```

| Email | From | What happens |
| --- | --- | --- |
| "Your Ventura Water eBill is Now Available" | `noreply@kubra.com` | A **bill** is recorded: amount due and due date. |
| "Ventura Water Payment Service - Payment Confirmation" (autopay, status `SCHEDULED`) | `noreply@kubra.com` | The **payment** is recorded, including KUBRA's $2.95 fee, and matched to its bill. A **request** goes out for the roommate's share of the total. |
| "KUBRA EZ-PAY - Your payment processed successfully" (one-time payment) | `noreply@kubra.com` | Same as an autopay confirmation. |
| "Bill is Ready" | `sce@message.sce.com` | A **bill** is recorded. On statement date + `SCE_AUTOPAY_LAG_DAYS` (10), the autopay date, the app assumes the bill's amount was paid and sends the request. |
| Anything payment-like from SCE | `*.sce.com` | Forwarded to you as "can't read yet". SCE's autopay confirmation hasn't been seen yet; when it arrives, add a parser (see below). Once it has one, a confirmation replaces the assumed payment. If the amount differs, the request is corrected, or you're warned if you already sent it. |
| "Austin Will paid your $X request", or "Austin Will paid $X to your Venmo account" if they pay without a request | `venmo@venmo.com` | The open request for that amount is marked **paid** and you get a ✅. Other Venmo payments, like rent, don't count. |

The share is `SPLIT_PERCENT` (50%) of what actually left the account, fees
included: Ventura Water $57.19 becomes $28.60, rounded half-up to the cent.

**Rent** isn't driven by an email. With `RENT_TOTAL=2900`, at 9am on the
last day of each month you get a request for $1,450.00 with the note "Rent
November 2026": it's charged at the end of the month for the month after.
Rent gets the same buttons, reminders and settling as a bill. If the bot was
off that day, it catches up within 7 days. Rent still goes out when Gmail
can't be read.

If a request isn't marked requested or skipped, it's sent again once a day,
up to 3 times. If a bill passes its due date with no payment seen, you get
one "is autopay still on?" alert. If Gmail can't be read (for example, the
sign-in expired), you're told once per distinct problem.

| Module | Holds |
| --- | --- |
| `parsers.py` | One pure function per email format: `Email` → `BillReady` / `PaymentMade` / `RoommatePaid` / `Unrecognized` |
| `pipeline.py` | Applying an event to the database: matching payments to bills, assuming SCE autopay, charging rent, what's due to send |
| `split.py` | Share rounding and the Venmo link |
| `messages.py` | The Telegram text |
| `service.py` | One poll (fetch → parse → record) and the outbox (send → mark delivered) |
| `gmail.py` / `login.py` | Read-only Gmail API client, token refresh, and the one-time browser sign-in |
| `bot.py` | The Telegram bot: allowlist, poll loop, buttons, `/status` `/check` `/backfill` |
| `db/` | SQLAlchemy models and engine; migrations live in `migrations/` (Alembic) |

### Why Venmo links rather than the Venmo API

Venmo has no public API for requesting money, and Zelle has no consumer API
at all. The unofficial Venmo clients break without warning and can get an
account flagged. Instead, the app builds Venmo's own web link:

```
https://venmo.com/?txn=charge&audience=private&recipients=AGWill&amount=28.60&note=Ventura%20Water%20Oct%202026%20(%2457.19%20total)
```

On a phone, this link opens the Venmo app on a prefilled request. Telegram
buttons only accept `https` links, which is why it isn't the `venmo://` form.

## Run it yourself

The bot only polls while it's running, so run it on a machine that stays on.
It catches up on up to 7 days of missed mail when it restarts.

1. **Google OAuth client** (one time):
   1. In [Google Cloud Console](https://console.cloud.google.com/), create a
      project and enable the **Gmail API**.
   2. Under **APIs & Services → OAuth consent screen**, choose External, add
      yourself as a test user, then **Publish app** so the status is "In
      production". In "Testing", refresh tokens expire after 7 days and you'd
      have to sign in again every week. The app stays unverified, which is
      fine for your own account: you'll click past one warning on sign-in.
   3. Under **Credentials → Create credentials → OAuth client ID**, choose
      **Desktop app**. Download the JSON to
      `~/.config/pay-my-bills/client_secret.json`.
2. **Telegram bot**: create one with @BotFather, and get your user id from
   @userinfobot.
3. **Configure**: `cp apps/pay-my-bills/.env.example apps/pay-my-bills/.env`
   and fill it in.
4. **Sign in to Gmail** (opens a browser; asks only for read access):
   ```bash
   cd apps/pay-my-bills
   uv run --package pay-my-bills pay-my-bills-login
   ```
5. **Start the bot** (it migrates the SQLite database itself):
   ```bash
   uv run --package pay-my-bills pay-my-bills
   ```
6. In Telegram, send `/backfill 45` to pick up recent bills, then `/status`.
   Close anything already settled with its **✅ Paid** (or **Skip**) button.

Each request has **✔️ Mark requested**, **✅ Paid** and **Skip** buttons.
Venmo payments are marked paid on their own from Venmo's email; use **✅
Paid** for anything paid another way (Zelle, cash, a request you sent by
hand). `/status` lists everything open with the same Paid / Skip buttons, so
nothing has to be found by scrolling back to its message.

Run the commands from `apps/pay-my-bills` so its `.env` is the one that gets
loaded.

## Adding the SCE autopay format

When the bot forwards an SCE email it can't read:

1. Save the email's text (Gmail → ⋮ → Show original, or the Gmail connector's
   plain-text view) to `tests/fixtures/sce_payment.txt`, with the account
   number redacted.
2. In `parsers._sce`, return a `PaymentMade(utility=ELECTRIC, ...)` for it,
   then add a test next to `test_sce_bill_ready`.

From then on, the confirmation takes over from the assumed payment
automatically.

## Development

```bash
uv run --package pay-my-bills pytest apps/pay-my-bills
uv run ruff check apps/pay-my-bills

# After changing db/models.py:
cd apps/pay-my-bills
uv run --package pay-my-bills alembic revision --autogenerate -m "what changed"
```

Tests run against a throwaway SQLite file that's migrated with Alembic, so the
migrations are tested too. The email fixtures are the real messages with
account numbers and the service address redacted.
