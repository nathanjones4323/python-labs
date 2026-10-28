"""Read-only Gmail: recreation.gov's recent alert emails, as HTML.

The same arrangement as pay-my-bills: the OAuth consent happens once, in
`campsite-booker-gmail-login` (a browser is needed). After that the watcher
only loads the saved token and refreshes it, writing the refreshed token
back. When Google stops accepting it every call raises
`AuthenticationRequired`, and the fix is to log in again. The scope is
`gmail.readonly`: the app can't send, delete or label mail.

The OAuth client can be pay-my-bills' (one Google Cloud project for both);
the token is this app's own.
"""

import base64
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from campsite_booker.alerts import GMAIL_QUERY
from campsite_booker.config import GmailSettings

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
LOGIN_HINT = (
    "Run `uv run --package campsite-booker campsite-booker-gmail-login` to sign in."
)


class AuthenticationRequired(Exception):
    pass


@dataclass(frozen=True)
class Message:
    gmail_id: str
    subject: str
    # When Gmail received it.
    received_at: datetime
    html: str


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

    def recent_ids(self, days: int = 1) -> list[str]:
        response = self._call(
            self._service.users()
            .messages()
            .list(userId="me", q=f"{GMAIL_QUERY} newer_than:{days}d", maxResults=50)
        )
        return [m["id"] for m in response.get("messages", [])]

    def fetch(self, gmail_id: str) -> Message:
        message = self._call(
            self._service.users()
            .messages()
            .get(userId="me", id=gmail_id, format="full")
        )
        return to_message(message)

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


def to_message(message: dict) -> Message:
    """A Gmail API message (format=full): subject, received time, HTML."""
    payload = message.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    return Message(
        gmail_id=message["id"],
        subject=headers.get("subject", ""),
        received_at=datetime.fromtimestamp(int(message["internalDate"]) / 1000, UTC),
        html=_html(payload),
    )


def _html(part: dict) -> str:
    data = part.get("body", {}).get("data")
    if data and part.get("mimeType") == "text/html":
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        return raw.decode("utf-8", errors="replace")
    for child in part.get("parts", []):
        if html := _html(child):
            return html
    return ""
