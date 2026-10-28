"""`campsite-booker-login`: sign in to recreation.gov once, in a window you
can see, and save the session for holds to reuse.

    campsite-booker-login           # sign in (fills RECGOV_EMAIL / _PASSWORD)
    campsite-booker-login --check   # is the saved session still signed in?
"""

import argparse
import sys

from campsite_booker import booker
from campsite_booker.config import Settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="campsite-booker-login")
    parser.add_argument(
        "--check", action="store_true", help="only check the saved session"
    )
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    if args.check:
        ok = booker.check_session(settings)
        print("Signed in." if ok else "Signed out: run campsite-booker-login.")
        sys.exit(0 if ok else 1)
    booker.interactive_login(settings)
    print(f"Saved the session to {settings.session_file}")


if __name__ == "__main__":
    main()
