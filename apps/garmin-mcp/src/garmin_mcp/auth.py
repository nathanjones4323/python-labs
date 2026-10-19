"""The Garmin session: restored from the token store, never from a password.

Signing in can need an MFA code typed by a person, which a stdio server has
no way to ask for, so it happens in `garmin-mcp-login` (see `login`). The
server only ever loads the tokens that wrote. python-garminconnect refreshes
them as they near expiry and writes the refreshed pair back to the same file,
so a session lasts until Garmin revokes the refresh token; then every tool
reports `authentication` and the fix is to run the login again.
"""

import logging
import os
import stat
import threading
from collections.abc import Callable
from pathlib import Path

from garminconnect import Garmin, GarminConnectTooManyRequestsError

from garmin_mcp.config import Settings
from garmin_mcp.errors import AuthenticationRequired, NetworkError, RateLimited
from garmin_mcp.repositories.failures import REAUTH, network_cause

log = logging.getLogger(__name__)


def secure_tokenstore(directory: Path) -> None:
    """Make the token store owner-only: 0700 for the directory, 0600 for the
    files in it. The library writes them that way; this catches a store
    copied in from elsewhere or loosened by hand."""
    if not directory.exists():
        return
    if directory.is_symlink():
        raise AuthenticationRequired(
            f"The token store {directory} is a symlink, which the Garmin "
            f"library refuses. Point GARMIN_TOKENSTORE at a real directory."
        )
    _restrict(directory, 0o700)
    for child in directory.iterdir():
        if child.is_file() and not child.is_symlink():
            _restrict(child, 0o600)


def _restrict(path: Path, mode: int) -> None:
    current = stat.S_IMODE(path.stat().st_mode)
    if current & 0o077:
        log.warning("Tightening permissions on %s from %o to %o", path, current, mode)
        os.chmod(path, mode)


class GarminSession:
    """One logged-in `Garmin` client, created on first use and dropped when
    Garmin stops accepting it."""

    def __init__(
        self, settings: Settings, factory: Callable[[], Garmin] | None = None
    ) -> None:
        self._settings = settings
        self._factory = factory or (
            lambda: Garmin(retry_attempts=settings.read_retries)
        )
        self._client: Garmin | None = None
        self._lock = threading.Lock()

    def client(self) -> Garmin:
        with self._lock:
            if self._client is None:
                self._client = self._restore()
            return self._client

    def invalidate(self) -> None:
        with self._lock:
            self._client = None

    def _restore(self) -> Garmin:
        token_file = self._settings.token_file
        if not token_file.exists():
            raise AuthenticationRequired(
                f"No saved Garmin session at {token_file}. {REAUTH}"
            )
        secure_tokenstore(self._settings.tokenstore)
        garmin = self._factory()
        try:
            # No email or password on the client: if the tokens don't work,
            # this fails instead of attempting a credential login.
            garmin.login(str(self._settings.tokenstore))
        except GarminConnectTooManyRequestsError as exc:
            raise RateLimited() from exc
        except Exception as exc:
            if network_cause(exc) is not None:
                raise NetworkError(
                    "Couldn't reach Garmin Connect to restore the session."
                ) from exc
            # Anything else from a token-only login means the tokens didn't
            # work; the library reports an unreadable token file as a plain
            # connection error, for one.
            raise AuthenticationRequired(
                f"Garmin didn't accept the saved session. {REAUTH}"
            ) from exc
        log.info("Garmin session restored from the token store")
        return garmin
