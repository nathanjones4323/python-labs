"""Work out how a venue takes reservations by reading its own website.

Venues say this plainly on their site — a Resy widget, an OpenTable button,
a "Reserve entry time" link to their own ticketing, or a line of prose
telling you to call. So: fetch the homepage, follow at most a few
reservation-looking links, and look for the signals below in priority order.
A platform link always beats prose, because a site that says "call us" in the
footer and embeds Resy on /reservations takes Resy. Timed-entry ticketing on
the venue's own domain (the Getty Villa's `tickets.getty.edu`) comes next.

Separately, a page saying reservations are *required* is noted at the end of
the evidence (`REQUIRED_NOTE`), along with any party size the rule is limited
to, and `required_from` reads it back for `suggest_reservation_need`.

Nothing here writes to the database; `tools.venues` decides what to
cache. Detection never raises on a 404 or a timeout for one page — only an
unreachable site (`DetectionFailed`) stops it.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import time
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.lexbor import LexborHTMLParser

from date_planner.booking.channels import BookingChannel, WindowUnit
from date_planner.booking.window import BookingWindow
from date_planner.config import Settings, get_settings
from date_planner.errors import DetectionFailed

# Paths worth trying even when nothing on the homepage links to them.
GUESS_PATHS = ("/reservations", "/reserve", "/book")
# Anchor text or href that looks like the way in to booking.
RESERVATION_LINK = re.compile(r"reserv|book|ticket", re.I)

# --- platform signals ------------------------------------------------------

RESY_VENUE = re.compile(r"resy\.com/cities/([a-z0-9-]+)/venues/([a-z0-9-]+)", re.I)
RESY_WIDGET = re.compile(r"widgets\.resy\.com", re.I)
# `opentable.com/r/<slug>`, optionally under a locale prefix (`/en-ca/r/...`).
OPENTABLE_SLUG = re.compile(
    r"opentable\.(?:com|co\.uk|ca)(?:/[a-z]{2}(?:-[a-z]{2})?)?/r/([a-z0-9-]+)", re.I
)
OPENTABLE_RID = re.compile(
    r"opentable\.[a-z.]+/restref/[^\"'\s<>]*?[?&]rid=(\d+)", re.I
)
OPENTABLE_WIDGET = re.compile(r"opentable\.[a-z.]+/widget/reservation", re.I)
TOCK_VENUE = re.compile(r"exploretock\.com/([a-z0-9_-]+)", re.I)
SEVENROOMS_VENUE = re.compile(
    r"sevenrooms\.com/(?:reservations|explore)/([a-z0-9_-]+)", re.I
)
# exploretock.com paths that are Tock's own pages, not a venue.
TOCK_NON_VENUE = frozenset(
    {
        "about",
        "blog",
        "business",
        "careers",
        "company",
        "gift",
        "help",
        "press",
        "privacy",
        "search",
        "signup",
        "terms",
    }
)

PHONE_PROSE = re.compile(
    r"reservations?.{0,40}?(?:by|via|over the|through)\s+(?:phone|telephone|calling)"
    r"|(?:please\s+)?call\s+(?:us\s+)?.{0,30}?(?:for|to\s+(?:make|book|request))"
    r"\s+(?:a\s+)?reservation",
    re.I | re.S,
)
WALK_IN_PROSE = re.compile(
    r"walk[\s-]?ins?\s+only|no\s+reservations|first[\s-]come", re.I
)
TEL_LINK = re.compile(r"tel:([+0-9().\s-]{7,})", re.I)

# --- timed-entry signals ---------------------------------------------------

# Link text that means "book a time slot here", as museums word it. Plain
# "tickets" isn't enough: a restaurant's events page says that too.
TICKET_LINK_TEXT = re.compile(
    r"timed[\s-]?entry|reserve\s+(?:your\s+|an?\s+)?(?:entry|tickets?|time)"
    r"|entry\s+time|(?:buy|get|book)\s+(?:your\s+)?tickets?|admission\s+tickets?",
    re.I,
)
TIMED_ENTRY_PROSE = re.compile(r"timed[\s-]?entry", re.I)
REQUIRED_PROSE = re.compile(
    # Not "no reservations required", which says the opposite.
    r"(?<!\bno\s)reservations?\s+(?:are\s+|is\s+)?required"
    r"|requires?\s+(?:an?\s+)?(?:advance\s+)?(?:timed[\s-]?entry\s+)?reservations?",
    re.I,
)
# Appended to the evidence when a page says booking is required.
REQUIRED_NOTE = "reservations required"
# The rule is often only for big groups: "Reservations are required for
# parties of 6+", "Parties of 8 or more require a reservation". Read from the
# sentence around the match. "Large parties" with no number is taken as six.
PARTY_LIMIT = re.compile(
    r"(?:parties|groups)\s+(?:of\s+)?"
    r"(?:(?P<at_least>\d+)\s*(?:\+|or\s+(?:more|larger))"
    r"|(?:larger\s+than|more\s+than|over)\s+(?P<over>\d+))"
    r"|(?P<large>large\s+(?:parties|groups))",
    re.I,
)
LARGE_PARTY = 6
SENTENCE_END = re.compile(r"[.!?\n]")
# The note as `classify` writes it, for `required_from`.
REQUIRED_EVIDENCE = re.compile(
    re.escape(REQUIRED_NOTE) + r"(?: for parties of (\d+)\+)?:", re.I
)

# --- booking-window signals ------------------------------------------------

NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
WINDOW_AHEAD = re.compile(
    r"(\d{1,3}|" + "|".join(NUMBER_WORDS) + r")\s*"
    r"(day|week|month)s?\s+(?:in\s+advance|ahead|prior|out)",
    re.I,
)
# Nobu's phrasing: a calendar month, not 30 days.
WINDOW_CALENDAR_MONTH = re.compile(
    r"(\d{1,2}|one|two|three)\s+months?\s+(?:to|from)\s+the\s+"
    r"(?:exact\s+)?(?:calendar\s+)?date",
    re.I,
)
WINDOW_OPEN_TIME = re.compile(
    r"(?:lines?|phones?|reservations?|bookings?|we)\s+(?:are\s+|is\s+)?"
    r"(?:open|opens|available)\s+(?:daily\s+)?at\s+"
    r"(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)",
    re.I,
)


@dataclass(frozen=True)
class Detection:
    """What reading the site said, and what said it."""

    channel: BookingChannel
    confidence: float
    evidence: str
    platform_slug: str | None = None
    platform_city_slug: str | None = None
    phone: str | None = None
    # Only set when the site states its window in words this module parses.
    window: BookingWindow | None = None
    pages_fetched: tuple[str, ...] = ()


@dataclass
class Page:
    url: str
    html: str
    text: str = field(default="", repr=False)


def _normalize_website(website: str) -> str:
    website = website.strip()
    if not website:
        raise DetectionFailed("No website to read")
    if not urlparse(website).scheme:
        website = f"https://{website}"
    return website


def normalize_phone(raw: str) -> str | None:
    """A `tel:` href or printed number as E.164, assuming +1 for 10 digits."""
    digits = re.sub(r"\D", "", raw)
    if raw.strip().startswith("+") and len(digits) >= 8:
        return f"+{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    return None


def _text(html: str) -> str:
    # Collapse whitespace so a phrase split across tags and newlines still
    # matches one regex.
    return re.sub(r"\s+", " ", LexborHTMLParser(html).text(separator=" "))


def _candidate_urls(page: Page, limit: int) -> list[str]:
    """Reservation-looking links on `page`, then the conventional guesses."""
    origin = urlparse(page.url)
    found: list[str] = []
    for node in LexborHTMLParser(page.html).css("a[href]"):
        href = node.attributes.get("href") or ""
        label = node.text(strip=True) or ""
        if not RESERVATION_LINK.search(href) and not RESERVATION_LINK.search(label):
            continue
        absolute = urljoin(page.url, href)
        parsed = urlparse(absolute)
        # Only the restaurant's own pages are worth a fetch; a link straight
        # out to resy.com is already a signal on this page.
        if parsed.scheme not in ("http", "https") or parsed.netloc != origin.netloc:
            continue
        clean = absolute.split("#")[0]
        if clean != page.url and clean not in found:
            found.append(clean)
    for path in GUESS_PATHS:
        guess = urljoin(page.url, path)
        if guess != page.url and guess not in found:
            found.append(guess)
    return found[:limit]


async def _fetch(client: httpx.AsyncClient, url: str) -> Page | None:
    try:
        response = await client.get(url)
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    html = response.text
    return Page(url=str(response.url), html=html, text=_text(html))


def _find_phone(pages: Iterable[Page]) -> str | None:
    for page in pages:
        for match in TEL_LINK.finditer(page.html):
            phone = normalize_phone(match.group(1))
            if phone:
                return phone
    return None


def parse_window(text: str) -> BookingWindow | None:
    """The booking window a page states, if it states one this module reads."""
    amount: int | None = None
    unit: WindowUnit | None = None

    calendar = WINDOW_CALENDAR_MONTH.search(text)
    if calendar:
        amount, unit = _number(calendar.group(1)), WindowUnit.MONTHS
    else:
        ahead = WINDOW_AHEAD.search(text)
        if ahead:
            amount = _number(ahead.group(1))
            period = ahead.group(2).lower()
            if period == "month":
                unit = WindowUnit.MONTHS
            elif period == "week":
                amount, unit = amount * 7, WindowUnit.DAYS
            else:
                unit = WindowUnit.DAYS

    open_time = None
    clock = WINDOW_OPEN_TIME.search(text)
    if clock:
        hour = int(clock.group(1)) % 12
        minute = int(clock.group(2) or 0)
        if clock.group(3).lower().startswith("p"):
            hour += 12
        if hour < 24 and minute < 60:
            open_time = time(hour, minute)

    if amount is None or unit is None:
        return None
    return BookingWindow(amount=amount, unit=unit, open_time=open_time)


def _number(raw: str) -> int:
    return NUMBER_WORDS.get(raw.lower(), 0) or int(raw)


def _platform_signal(pages: list[Page]) -> Detection | None:
    """The first platform signal in priority order, over all fetched pages."""
    for page in pages:
        resy = RESY_VENUE.search(page.html)
        if resy:
            return Detection(
                channel=BookingChannel.RESY,
                confidence=0.95,
                evidence=f"Resy venue link on {page.url}: {resy.group(0)}",
                platform_city_slug=resy.group(1).lower(),
                platform_slug=resy.group(2).lower(),
            )
    for page in pages:
        if RESY_WIDGET.search(page.html):
            return Detection(
                channel=BookingChannel.RESY,
                # Below the skill's 0.6 action threshold: there's no slug to
                # build a link from, so this isn't something to act on
                # confidently, only something to ask the user about.
                confidence=0.5,
                evidence=f"Resy booking widget on {page.url}, but no venue slug",
            )
    for page in pages:
        slug = OPENTABLE_SLUG.search(page.html)
        if slug:
            return Detection(
                channel=BookingChannel.OPENTABLE,
                confidence=0.95,
                evidence=f"OpenTable link on {page.url}: {slug.group(0)}",
                platform_slug=slug.group(1).lower(),
            )
        rid = OPENTABLE_RID.search(page.html)
        if rid:
            return Detection(
                channel=BookingChannel.OPENTABLE,
                confidence=0.9,
                evidence=f"OpenTable restref on {page.url}: rid={rid.group(1)}",
                platform_slug=rid.group(1),
            )
        if OPENTABLE_WIDGET.search(page.html):
            return Detection(
                channel=BookingChannel.OPENTABLE,
                # Below the skill's 0.6 action threshold, same reasoning as
                # the Resy widget-only case above: a widget with no slug or
                # rid isn't something `build_booking_link` can use, so it
                # shouldn't look confident enough to act on. Real venues embed
                # this loader (`.../widget/reservation/loader?...rid=...`)
                # while still being effectively phone-only in practice — a
                # false positive this module can't safely resolve on its own.
                confidence=0.5,
                evidence=f"OpenTable reservation widget on {page.url}, no slug",
            )
    for page in pages:
        for match in TOCK_VENUE.finditer(page.html):
            venue = match.group(1).lower()
            if venue in TOCK_NON_VENUE:
                continue
            return Detection(
                channel=BookingChannel.TOCK,
                confidence=0.95,
                evidence=f"Tock link on {page.url}: {match.group(0)}",
                platform_slug=venue,
            )
    for page in pages:
        seven = SEVENROOMS_VENUE.search(page.html)
        if seven:
            return Detection(
                channel=BookingChannel.SEVENROOMS,
                confidence=0.9,
                evidence=f"SevenRooms link on {page.url}: {seven.group(0)}",
                platform_slug=seven.group(1).lower(),
            )
    return None


def _site_domain(netloc: str) -> str:
    """`tickets.getty.edu` and `www.getty.edu` -> `getty.edu`."""
    host = netloc.lower().split(":")[0]
    return ".".join(host.split(".")[-2:])


def _ticket_signal(pages: list[Page]) -> Detection | None:
    """A timed-entry booking link to the venue's own domain."""
    for page in pages:
        home = _site_domain(urlparse(page.url).netloc)
        for node in LexborHTMLParser(page.html).css("a[href]"):
            label = node.text(strip=True) or ""
            if not TICKET_LINK_TEXT.search(label):
                continue
            target = urljoin(page.url, node.attributes.get("href") or "")
            parsed = urlparse(target)
            if parsed.scheme not in ("http", "https"):
                continue
            if _site_domain(parsed.netloc) != home:
                continue
            timed = any(TIMED_ENTRY_PROSE.search(p.text) for p in pages)
            return Detection(
                channel=BookingChannel.TICKETED,
                confidence=0.85 if timed else 0.7,
                evidence=f'"{label}" link on {page.url} to {target}',
                # Too long for the column is too long to be a booking page.
                platform_slug=target if len(target) <= 200 else None,
            )
    return None


def _prose_signal(pages: list[Page], phone: str | None) -> Detection | None:
    """Phone-only or walk-in, read from the words on the page."""
    for page in pages:
        prose = PHONE_PROSE.search(page.text)
        if prose:
            return Detection(
                channel=BookingChannel.PHONE,
                confidence=0.8,
                evidence=f'"{prose.group(0).strip()}" on {page.url}',
            )
    for page in pages:
        walk_in = WALK_IN_PROSE.search(page.text)
        if walk_in:
            return Detection(
                channel=BookingChannel.WALK_IN,
                confidence=0.7,
                evidence=f'"{walk_in.group(0).strip()}" on {page.url}',
            )
    # A reservations page whose only way to book is a phone number.
    if phone is not None:
        for page in pages[1:]:
            if RESERVATION_LINK.search(page.url) and TEL_LINK.search(page.html):
                return Detection(
                    channel=BookingChannel.PHONE,
                    confidence=0.6,
                    evidence=(
                        f"{page.url} offers only a phone number ({phone}) "
                        "and no booking platform"
                    ),
                )
    return None


def _sentence(text: str, match: re.Match[str]) -> str:
    """The sentence `match` sits in. Page text runs block elements together,
    so this can take in a heading before it; it's only searched, never
    quoted."""
    start = max(
        (m.end() for m in SENTENCE_END.finditer(text, 0, match.start())), default=0
    )
    end = SENTENCE_END.search(text, match.end())
    sentence = text[start : end.end() if end else len(text)]
    return " ".join(sentence.split())


def _party_minimum(sentence: str) -> int | None:
    """The smallest party a "reservations required" sentence applies to,
    or None when it applies to everyone."""
    limit = PARTY_LIMIT.search(sentence)
    if limit is None:
        return None
    if limit["at_least"]:
        return int(limit["at_least"])
    if limit["over"]:
        return int(limit["over"]) + 1
    return LARGE_PARTY


def required_from(evidence: str | None) -> int | None:
    """Read back the note `classify` adds: the smallest party that must
    book (1 for everyone), or None when the site didn't say booking is
    required."""
    found = REQUIRED_EVIDENCE.search(evidence or "")
    if found is None:
        return None
    return int(found[1]) if found[1] else 1


def classify(pages: list[Page]) -> Detection:
    """The channel these pages point to, platform signals first."""
    phone = _find_phone(pages)
    window = next(
        (w for w in (parse_window(page.text) for page in pages) if w is not None),
        None,
    )
    detection = (
        _platform_signal(pages) or _ticket_signal(pages) or _prose_signal(pages, phone)
    )
    if detection is None:
        detection = Detection(
            channel=BookingChannel.UNKNOWN,
            confidence=0.0,
            evidence=(
                "No platform link, phone instruction or walk-in note on "
                + ", ".join(page.url for page in pages)
            ),
        )
    required = next(
        ((page.text, m) for page in pages if (m := REQUIRED_PROSE.search(page.text))),
        None,
    )
    if required is not None:
        text, match = required
        note = REQUIRED_NOTE
        minimum = _party_minimum(_sentence(text, match))
        if minimum is not None:
            note = f"{note} for parties of {minimum}+"
        detection = replace(
            detection, evidence=f'{detection.evidence}; {note}: "{match.group(0)}"'
        )
    return replace(
        detection,
        phone=phone,
        window=window,
        pages_fetched=tuple(page.url for page in pages),
    )


async def detect(
    website: str,
    *,
    client: httpx.AsyncClient | None = None,
    settings: Settings | None = None,
) -> Detection:
    """Read `website` (plus a few reservation pages) and classify it."""
    settings = settings or get_settings()
    url = _normalize_website(website)
    if client is not None:
        return await _detect_with(client, url, settings)
    async with httpx.AsyncClient(
        timeout=settings.fetch_timeout,
        follow_redirects=True,
        headers={"User-Agent": settings.user_agent},
    ) as owned:
        return await _detect_with(owned, url, settings)


async def _detect_with(
    client: httpx.AsyncClient, url: str, settings: Settings
) -> Detection:
    home = await _fetch(client, url)
    if home is None:
        raise DetectionFailed(f"Couldn't fetch {url}")
    pages = [home]
    budget = max(settings.max_pages - 1, 0)
    for candidate in _candidate_urls(home, budget):
        if len(pages) > budget:
            break
        page = await _fetch(client, candidate)
        if page is not None and page.url not in {p.url for p in pages}:
            pages.append(page)
    return classify(pages)
