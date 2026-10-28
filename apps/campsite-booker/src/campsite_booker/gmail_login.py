"""`campsite-booker-gmail-login`: sign in to Gmail once, in a browser, so
the watcher can read recreation.gov's alert emails.

Opens Google's consent screen for read-only Gmail access using the Desktop
OAuth client (pay-my-bills' by default, see `GmailSettings`), and saves the
token the watcher then uses on its own. Run it again whenever the watcher
says to.
"""

import sys

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from campsite_booker.config import GmailSettings
from campsite_booker.gmail import SCOPES, save_token


def main() -> None:
    settings = GmailSettings.from_env()
    if not settings.client_secrets.exists():
        sys.exit(
            f"No OAuth client at {settings.client_secrets}. Use pay-my-bills' "
            "(~/.config/pay-my-bills/client_secret.json), or create a Desktop OAuth "
            "client in Google Cloud and point CAMPSITE_GMAIL_CLIENT_SECRETS at it."
        )
    flow = InstalledAppFlow.from_client_secrets_file(
        str(settings.client_secrets), SCOPES
    )
    # prompt=consent: always hand back a refresh token, even on a re-login.
    credentials = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    save_token(credentials, settings.token_file)
    profile = (
        build("gmail", "v1", credentials=credentials, cache_discovery=False)
        .users()
        .getProfile(userId="me")
        .execute()
    )
    print(
        f"Signed in as {profile['emailAddress']}; token saved to {settings.token_file}"
    )


if __name__ == "__main__":
    main()
