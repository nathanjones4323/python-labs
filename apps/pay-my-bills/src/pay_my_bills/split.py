"""The roommate's share of a payment, and the Venmo link that asks for it.

Venmo has no API for requesting money, so the app builds Venmo's own web
link with the request prefilled. On a phone it opens the Venmo app on the
request screen (recipient, amount and note filled in) and the person presses
Request; nothing is sent without them.
"""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import quote, urlencode

VENMO_URL = "https://venmo.com/"
# Venmo's redirect to the app re-encodes a space ("%20") as "+", which the
# app then shows literally: "SCE+Oct+2026". A no-break space comes through
# as itself and looks the same.
NO_BREAK_SPACE = "\u00a0"


def share_cents(total_cents: int, percent: int) -> int:
    """`percent` of the total, to the cent, half a cent rounding up: half of
    $57.19 is $28.60."""
    exact = Decimal(total_cents) * percent / 100
    return int(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def dollars(cents: int) -> str:
    return f"{cents // 100:,}.{cents % 100:02d}"


def request_note(utility_name: str, billed: date, total_cents: int) -> str:
    return f"{utility_name} {billed:%b %Y} (${dollars(total_cents)} total)"


def venmo_request_url(username: str, amount_cents: int, note: str) -> str:
    query = urlencode(
        {
            "txn": "charge",
            "audience": "private",
            "recipients": username,
            "amount": dollars(amount_cents).replace(",", ""),
            "note": note.replace(" ", NO_BREAK_SPACE),
        },
        quote_via=quote,
    )
    return f"{VENMO_URL}?{query}"
