"""`garmin-mcp-login`: sign in to Garmin once, interactively, and save the
session tokens the server runs on.

    uv run --package garmin-mcp garmin-mcp-login [--email you@example.com]

Asks for the password (never echoed, never stored) and, if the account has
two-factor authentication, the code Garmin sends. The tokens go to
`$GARMIN_TOKENSTORE/garmin_tokens.json`, 0600 in a 0700 directory. Run it
again whenever the server reports `authentication`; it replaces the old
tokens. Restart the MCP server afterwards.
"""

import argparse
import getpass
import os
import sys

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from garmin_mcp.auth import secure_tokenstore
from garmin_mcp.config import get_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="garmin-mcp-login", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--email", help="defaults to GARMIN_EMAIL, else asks")
    args = parser.parse_args(argv)
    settings = get_settings()

    # The password and the MFA code have to be typed by a person. Without a
    # terminal (Claude Code's `!` commands, a pipe, cron) the prompts would
    # just hit end-of-input.
    if not sys.stdin.isatty():
        print(
            "garmin-mcp-login needs an interactive terminal for the password "
            "and MFA prompts. Run it in a terminal window, from apps/garmin-mcp:\n"
            "    uv run --package garmin-mcp garmin-mcp-login",
            file=sys.stderr,
        )
        return 2

    email = args.email or settings.email or input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password: ")
    # Always a fresh credential login: the library would otherwise reuse
    # tokens from $GARMINTOKENS, which defeats signing in again.
    os.environ.pop("GARMINTOKENS", None)
    garmin = Garmin(
        email, password, prompt_mfa=lambda: input("Garmin MFA code: ").strip()
    )
    del password
    try:
        garmin.login()
    except GarminConnectTooManyRequestsError:
        print(
            "Garmin is rate limiting sign-ins. Wait a few minutes and try again.",
            file=sys.stderr,
        )
        return 1
    except GarminConnectAuthenticationError as exc:
        print(f"Sign-in failed: {exc}", file=sys.stderr)
        return 1
    except GarminConnectConnectionError as exc:
        print(f"Couldn't reach Garmin: {exc}", file=sys.stderr)
        return 1

    settings.tokenstore.mkdir(mode=0o700, parents=True, exist_ok=True)
    garmin.client.dump(str(settings.tokenstore))
    secure_tokenstore(settings.tokenstore)
    print(
        f"Signed in as {garmin.display_name}. Session saved to {settings.token_file}."
    )
    print("Restart the MCP server (in Claude Code: /mcp, then reconnect garmin).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
