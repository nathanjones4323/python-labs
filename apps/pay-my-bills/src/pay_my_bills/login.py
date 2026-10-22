"""`pay-my-bills-login`: sign in to Gmail once, in a browser.

Opens Google's consent screen for read-only Gmail access using the Desktop
OAuth client in GMAIL_CLIENT_SECRETS, and saves the token the bot then uses
on its own (see `gmail`). Run it again whenever the bot says to.
"""

import sys

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from pay_my_bills.config import GmailSettings
from pay_my_bills.gmail import SCOPES, save_token


def main() -> None:
    settings = GmailSettings.from_env()
    if not settings.client_secrets.exists():
        sys.exit(
            f"No OAuth client at {settings.client_secrets}. Create a Desktop OAuth "
            "client in Google Cloud and download its JSON there (see the README), "
            "or point GMAIL_CLIENT_SECRETS at it."
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
