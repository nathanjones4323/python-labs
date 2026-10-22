"""Read-only Gmail: list the billers' recent messages and fetch them as text.

The OAuth consent happens once, in `pay-my-bills-login` (a browser is
needed). After that the bot only loads the saved token and refreshes it,
writing the refreshed token back. When Google stops accepting it (revoked,
or the OAuth app left in "Testing", whose tokens die after 7 days) every
call raises `AuthenticationRequired` and the fix is to log in again.

The scope is `gmail.readonly`: the app can't send, delete or label mail.

Polling a narrow search every couple of minutes, and skipping the ids
already in the database, stands in for a push subscription: no Pub/Sub
topic, no public endpoint, and a missed poll is caught up by the next.
"""

import base64
import os
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from pay_my_bills.config import GmailSettings
from pay_my_bills.parsers import Email

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
# Ventura Water (KUBRA), SCE, and Venmo's transaction emails (not its
# marketing, which comes from email.venmo.com).
BILLERS_QUERY = "from:(kubra.com OR sce.com OR venmo@venmo.com)"
LOGIN_HINT = "Run `uv run --package pay-my-bills pay-my-bills-login` to sign in again."


class AuthenticationRequired(Exception):
    pass


def save_token(credentials: Credentials, token_file: Path) -> None:
    """Owner-only: 0700 for the directory, 0600 for the token."""
    token_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(token_file.parent, 0o700)
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(credentials.to_json())
    os.chmod(token_file, 0o600)


def load_credentials(settings: GmailSettings) -> Credentials:
    if not settings.token_file.exists():
        raise AuthenticationRequired(
            f"No Gmail sign-in saved at {settings.token_file}. {LOGIN_HINT}"
        )
    credentials = Credentials.from_authorized_user_file(
        str(settings.token_file), SCOPES
    )
    if credentials.valid:
        return credentials
    if not credentials.refresh_token:
        raise AuthenticationRequired(
            f"The saved Gmail sign-in can't refresh. {LOGIN_HINT}"
        )
    try:
        credentials.refresh(Request())
    except RefreshError as exc:
        raise AuthenticationRequired(
            f"Google refused the saved Gmail sign-in ({exc}). {LOGIN_HINT}"
        ) from exc
    save_token(credentials, settings.token_file)
    return credentials


class Gmail:
    """One poll's worth of Gmail calls. Made fresh for each poll: the client
    underneath isn't thread-safe, and each poll runs on a worker thread."""

    def __init__(self, settings: GmailSettings) -> None:
        self._service = build(
            "gmail", "v1", credentials=load_credentials(settings), cache_discovery=False
        )

    def recent_ids(self, days: int) -> list[str]:
        ids: list[str] = []
        page = None
        while True:
            response = self._call(
                self._service.users()
                .messages()
                .list(
                    userId="me",
                    q=f"{BILLERS_QUERY} newer_than:{days}d",
                    pageToken=page,
                    maxResults=100,
                )
            )
            ids.extend(m["id"] for m in response.get("messages", []))
            page = response.get("nextPageToken")
            if not page:
                return ids

    def fetch(self, gmail_id: str) -> Email:
        message = self._call(
            self._service.users()
            .messages()
            .get(userId="me", id=gmail_id, format="full")
        )
        return to_email(message)

    @staticmethod
    def _call(request):
        try:
            return request.execute(num_retries=3)
        except RefreshError as exc:
            raise AuthenticationRequired(
                f"Google refused the saved Gmail sign-in ({exc}). {LOGIN_HINT}"
            ) from exc
        except HttpError as exc:
            if exc.resp.status in (401, 403):
                raise AuthenticationRequired(
                    f"Gmail refused access ({exc.resp.status}). {LOGIN_HINT}"
                ) from exc
            raise


def to_email(message: dict) -> Email:
    """A Gmail API message (format=full) as sender, subject and plain text."""
    payload = message.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    plain, html = _bodies(payload)
    return Email(
        gmail_id=message["id"],
        sender=headers.get("from", ""),
        subject=headers.get("subject", ""),
        received_at=datetime.fromtimestamp(int(message["internalDate"]) / 1000, UTC),
        text=plain if plain.strip() else html_to_text(html),
    )


def _bodies(part: dict) -> tuple[str, str]:
    """The first text/plain and the first text/html body under `part`."""
    plain = html = ""
    mime = part.get("mimeType", "")
    data = part.get("body", {}).get("data")
    if data and mime == "text/plain":
        plain = _decode(data)
    elif data and mime == "text/html":
        html = _decode(data)
    for child in part.get("parts", []):
        child_plain, child_html = _bodies(child)
        plain = plain or child_plain
        html = html or child_html
    return plain, html


def _decode(data: str) -> str:
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    return raw.decode("utf-8", errors="replace")


_BLOCKS = {"p", "div", "br", "tr", "table", "li", "h1", "h2", "h3", "h4", "h5", "h6"}
_CELLS = {"td", "th"}
_SKIP = {"style", "script", "head", "title"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag, attrs) -> None:
        if tag in _SKIP:
            self._skipping += 1
        elif tag in _BLOCKS:
            self.parts.append("\n")
        elif tag in _CELLS:
            self.parts.append(" | ")

    def handle_endtag(self, tag) -> None:
        if tag in _SKIP:
            self._skipping = max(self._skipping - 1, 0)
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data) -> None:
        if not self._skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Enough of an HTML email as text for the parsers: block elements on
    their own lines, table cells separated by `|`."""
    extractor = _TextExtractor()
    extractor.feed(html)
    lines = (" ".join(line.split()) for line in "".join(extractor.parts).splitlines())
    return "\n".join(line for line in lines if line.strip(" |"))
