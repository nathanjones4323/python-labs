"""Session restore, token store permissions, and how library failures are
classified for reads and for writes. No network: `Garmin` is replaced by
small stand-ins, and the requests exceptions are built by hand."""

import os
from pathlib import Path
from typing import Any, cast

import pytest
import requests
from garmin_mcp.auth import GarminSession, secure_tokenstore
from garmin_mcp.config import Settings
from garmin_mcp.errors import (
    AuthenticationRequired,
    GarminApiError,
    InvalidInput,
    NetworkError,
    NotFound,
    RateLimited,
    WriteOutcomeUnknown,
    WriteRejected,
)
from garmin_mcp.repositories.failures import classify_read, classify_write
from garmin_mcp.repositories.garmin_client import GarminConnectRepository
from garmin_mcp.server import account_status, build_app
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)
from urllib3.exceptions import MaxRetryError, NewConnectionError


class StubGarmin:
    """Stands in for `garminconnect.Garmin`: records the login, then fails
    or succeeds as told."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.logins: list[tuple[Any, ...]] = []
        self.display_name = "runner"
        self.full_name = "A Runner"
        self.unit_system = "metric"
        self.username: str | None = None
        self.password: str | None = None

    def login(self, *args: Any) -> tuple[None, None]:
        self.logins.append(args)
        if self.error:
            raise self.error
        return None, None


def with_tokens(settings: Settings) -> Settings:
    settings.tokenstore.mkdir(parents=True)
    settings.token_file.write_text('{"di_token": "x"}')
    return settings


def response(code: int) -> requests.Response:
    r = requests.Response()
    r.status_code = code
    return r


def api_error(code: int) -> GarminConnectConnectionError:
    exc = GarminConnectConnectionError(f"API Error {code}")
    exc.response = response(code)
    return exc


def wrapped(cause: Exception) -> GarminConnectConnectionError:
    try:
        raise GarminConnectConnectionError("request failed") from cause
    except GarminConnectConnectionError as exc:
        return exc


def refused() -> requests.ConnectionError:
    reason = NewConnectionError(None, "connection refused")  # type: ignore[arg-type]
    return requests.ConnectionError(MaxRetryError(None, "/", reason))  # type: ignore[arg-type]


# -- session restore -------------------------------------------------------


def test_missing_tokens_ask_for_login(settings: Settings) -> None:
    session = GarminSession(
        settings, factory=lambda: pytest.fail("should not build a client")
    )
    with pytest.raises(AuthenticationRequired, match="garmin-mcp-login"):
        session.client()


def test_restore_uses_only_the_token_store(settings: Settings) -> None:
    stub = StubGarmin()
    session = GarminSession(with_tokens(settings), factory=lambda: stub)  # type: ignore[arg-type,return-value]
    assert session.client() is cast(Any, stub)
    assert stub.logins == [(str(settings.tokenstore),)]
    assert stub.username is None and stub.password is None
    # Restored once, then reused.
    session.client()
    assert len(stub.logins) == 1


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            GarminConnectAuthenticationError("Username and password are required"),
            AuthenticationRequired,
        ),
        (
            GarminConnectConnectionError("Token path not loading cleanly"),
            AuthenticationRequired,
        ),
        (GarminConnectTooManyRequestsError("slow down"), RateLimited),
        (wrapped(requests.ConnectTimeout()), NetworkError),
    ],
)
def test_restore_failures(
    settings: Settings, error: Exception, expected: type[Exception]
) -> None:
    session = GarminSession(with_tokens(settings), factory=lambda: StubGarmin(error))  # type: ignore[arg-type,return-value]
    with pytest.raises(expected):
        session.client()


def test_network_trouble_during_restore_is_not_an_auth_problem(
    settings: Settings,
) -> None:
    # The library reports a failed profile fetch as an authentication error
    # even when the network is what failed.
    try:
        raise GarminConnectAuthenticationError(
            "Failed to retrieve social profile"
        ) from requests.ReadTimeout()
    except GarminConnectAuthenticationError as exc:
        error = exc
    session = GarminSession(with_tokens(settings), factory=lambda: StubGarmin(error))  # type: ignore[arg-type,return-value]
    with pytest.raises(NetworkError):
        session.client()


def test_token_store_permissions_are_tightened(settings: Settings) -> None:
    with_tokens(settings)
    os.chmod(settings.tokenstore, 0o755)
    os.chmod(settings.token_file, 0o644)
    secure_tokenstore(settings.tokenstore)
    assert settings.tokenstore.stat().st_mode & 0o777 == 0o700
    assert settings.token_file.stat().st_mode & 0o777 == 0o600


def test_symlinked_token_store_is_refused(settings: Settings, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    settings.tokenstore.symlink_to(real)
    with pytest.raises(AuthenticationRequired, match="symlink"):
        secure_tokenstore(settings.tokenstore)


def test_account_status_without_tokens(settings: Settings) -> None:
    status = account_status(build_app(settings))
    assert not status.authenticated and not status.connected
    assert status.action and "garmin-mcp-login" in status.action
    assert status.dry_run is False


def test_repository_drops_a_rejected_session(settings: Settings) -> None:
    class Rejecting(StubGarmin):
        def get_workout_by_id(self, _id: int) -> Any:
            raise api_error(401)

    session = GarminSession(with_tokens(settings), factory=lambda: Rejecting())  # type: ignore[arg-type,return-value]
    repo = GarminConnectRepository(session)
    with pytest.raises(AuthenticationRequired):
        repo.get_workout(1)
    assert session._client is None  # noqa: SLF001


# -- classification --------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (GarminConnectTooManyRequestsError(), RateLimited),
        (GarminConnectAuthenticationError(), AuthenticationRequired),
        (api_error(401), AuthenticationRequired),
        (api_error(429), RateLimited),
        (api_error(500), GarminApiError),
        (wrapped(requests.ReadTimeout()), NetworkError),
        (requests.ConnectionError(), NetworkError),
        (ValueError("workout_id must be positive"), InvalidInput),
    ],
)
def test_classify_read(error: Exception, expected: type[Exception]) -> None:
    assert isinstance(classify_read(error, None), expected)


def test_classify_read_not_found_names_the_kind() -> None:
    error = classify_read(GarminConnectNotFoundError("API Error 404"), "workout")
    assert isinstance(error, NotFound) and error.kind == "workout"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        # Certainly not applied.
        (api_error(400), WriteRejected),
        (api_error(404), WriteRejected),
        (api_error(401), WriteRejected),
        (GarminConnectTooManyRequestsError(), WriteRejected),
        (requests.ConnectTimeout(), WriteRejected),
        (refused(), WriteRejected),
        (wrapped(refused()), WriteRejected),
        # Possibly applied.
        (api_error(500), WriteOutcomeUnknown),
        (api_error(503), WriteOutcomeUnknown),
        (requests.ReadTimeout(), WriteOutcomeUnknown),
        (wrapped(requests.ReadTimeout()), WriteOutcomeUnknown),
        (requests.ConnectionError("Connection reset by peer"), WriteOutcomeUnknown),
        (RuntimeError("anything else"), WriteOutcomeUnknown),
    ],
)
def test_classify_write(error: Exception, expected: type[Exception]) -> None:
    assert isinstance(classify_write(error), expected)
