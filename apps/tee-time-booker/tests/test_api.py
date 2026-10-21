"""The HTTP API over a real database, with the browser faked out.

Booking runs on the worker's thread, so a test that starts one waits on the
run's future before reading the outcome back through the API.
"""

import threading
import uuid
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from tee_time_booker import worker as worker_module
from tee_time_booker.api import create_app
from tee_time_booker.api.routers import plans as plans_router
from tee_time_booker.config import Settings
from tee_time_booker.models import BookingResult, TeeTime
from tee_time_booker.worker import BookingWorker

SLOT = TeeTime(
    platform="foreup",
    course_name="Soule Park",
    start=datetime(2026, 10, 3, 8, 0),
    green_fee=56.0,
    side="Front",
)


@pytest.fixture
def api(db, seeded, tmp_path):
    """A client over the seeded database, plus the worker to wait on."""
    settings = Settings(
        database_url=None,
        plan_id=None,
        submit_payment=False,
        headless=True,
        artifacts_dir=tmp_path / "artifacts",
        target_date_override=None,
    )
    worker = BookingWorker(db, settings, max_workers=2)
    app = create_app(settings, sessions=db, worker=worker)
    with TestClient(app) as client:
        client.worker = worker
        client.ids = seeded
        yield client


def wait_for(api, run_id: str):
    future = api.worker.futures.get(uuid.UUID(run_id))
    if future is not None:
        future.result(timeout=10)


# --- courses and plans -----------------------------------------------------


def test_courses_are_listed_and_fetched_by_key(api):
    courses = api.get("/courses").json()
    assert [c["key"] for c in courses] == ["saticoy", "soule-park"]
    soule = api.get("/courses/soule-park").json()
    assert soule["platform"] == "foreup" and soule["booking_window_days"] == 20
    assert soule["description"] is None and soule["green_fees"] is None
    saticoy = api.get("/courses/saticoy").json()
    assert saticoy["description"].startswith("A 9-hole")
    assert saticoy["green_fees"]["holes"] == 9
    assert saticoy["green_fees"]["weekday_low"] < saticoy["green_fees"]["weekend_high"]
    assert saticoy["green_fees"]["sampled_on"] == "2026-09-17"
    assert api.get("/courses/nope").status_code == 404


def test_plans_list_with_their_next_target_date(api):
    plans = api.get("/plans", params={"user_email": "golfer@example.com"}).json()
    assert {p["name"] for p in plans} == {"soule-saturday", "saticoy-saturday"}
    soule = next(p for p in plans if p["course"] == "soule-park")
    assert soule["env_prefix"] == "SOULE" and soule["weeks_ahead"] == 3
    assert date.fromisoformat(soule["next_target_date"]).weekday() == 5
    assert api.get(f"/plans/{soule['id']}").json() == soule
    assert api.get("/plans", params={"user_email": "x@example.com"}).status_code == 404


def test_create_plan_then_update_it_by_name(api):
    body = {
        "user_email": "golfer@example.com",
        "name": "soule-sunday",
        "course": "soule-park",
        "env_prefix": "SOULE",
        "target_weekday": 6,
        "weeks_ahead": 2,
        "earliest": "07:00",
        "latest": "10:00",
        "holes": 18,
        "players": 2,
    }
    created = api.post("/plans", json=body)
    assert created.status_code == 201, created.text
    plan_id = created.json()["id"]

    updated = api.post("/plans", json={**body, "players": 3}).json()
    assert updated["id"] == plan_id and updated["players"] == 3
    assert len(api.get("/plans").json()) == 3


def test_create_plan_rejects_bad_input(api):
    body = {
        "user_email": "golfer@example.com",
        "name": "x",
        "course": "saticoy",
        "env_prefix": "SOULE",
        "target_weekday": 6,
        "weeks_ahead": 1,
        "earliest": "07:00",
        "latest": "10:00",
        "holes": 9,
        "players": 2,
    }
    # A foreUP login can't book a GolfNow course.
    assert api.post("/plans", json=body).status_code == 422
    assert api.post("/plans", json={**body, "env_prefix": "NOPE"}).status_code == 404
    assert api.post("/plans", json={**body, "user_email": "x@y"}).status_code == 404
    assert api.post("/plans", json={**body, "latest": "06:00"}).status_code == 422
    assert api.post("/plans", json={**body, "target_weekday": 7}).status_code == 422


# --- finding ---------------------------------------------------------------


def test_find_tee_times_searches_the_plan_and_reports_slots(api, monkeypatch):
    seen = {}

    def find_tee_times(course, criteria, settings, *, account_env_prefix):
        seen.update(course=course, criteria=criteria, prefix=account_env_prefix)
        return [SLOT]

    monkeypatch.setattr(plans_router, "find_tee_times", find_tee_times)
    response = api.get(
        f"/plans/{api.ids['soule_plan']}/tee-times", params={"date": "2026-10-03"}
    )
    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "start": "2026-10-03T08:00:00",
            "holes": None,
            "players": None,
            "green_fee": 56.0,
            "service_fee": None,
            "side": "Front",
        }
    ]
    assert seen["course"].key == "soule-park" and seen["prefix"] == "SOULE"
    assert seen["criteria"].target_date == date(2026, 10, 3)
    assert seen["criteria"].players == 4


def test_find_tee_times_maps_site_and_date_errors(api, monkeypatch):
    url = f"/plans/{api.ids['soule_plan']}/tee-times"
    monkeypatch.setattr(
        plans_router,
        "find_tee_times",
        lambda *a, **k: (_ for _ in ()).throw(
            ValueError("target date 2020-01-01 is in the past")
        ),
    )
    assert api.get(url, params={"date": "2020-01-01"}).status_code == 422
    monkeypatch.setattr(
        plans_router,
        "find_tee_times",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Login failed (403)")),
    )
    response = api.get(url)
    assert response.status_code == 502 and "Login failed" in response.json()["detail"]


# --- booking ---------------------------------------------------------------


def test_book_plan_starts_a_background_run_and_records_a_dry_run(api, fake_booking):
    seen = {}
    fake_booking(BookingResult(tee_time=SLOT, submitted=False), seen)

    response = api.post(
        f"/plans/{api.ids['soule_plan']}/book", json={"target_date": "2026-10-03"}
    )
    assert response.status_code == 202, response.text
    started = response.json()
    assert started["course"] == "soule-park"
    assert started["target_date"] == "2026-10-03"
    assert started["dry_run"] is True
    # An explicit date is an already-open day: no release wait.
    assert started["wait_for_release"] is False

    wait_for(api, started["run_id"])
    run = api.get(f"/runs/{started['run_id']}").json()
    assert run["status"] == "dry_run"
    assert run["tee_time_local"] == "2026-10-03T08:00:00"
    assert run["side"] == "Front" and float(run["green_fee"]) == 56.0
    assert run["finished_at"] is not None
    request = api.get(f"/requests/{started['request_id']}").json()
    assert request["status"] == "pending"
    assert request["plan_id"] == str(api.ids["soule_plan"])
    assert seen["wait"] is False and seen["settings"].submit_payment is False


def test_book_plan_without_a_date_uses_the_plans_target_and_waits(api, fake_booking):
    seen = {}
    fake_booking(RuntimeError("stop"), seen)
    plan = api.get(f"/plans/{api.ids['soule_plan']}").json()

    started = api.post(f"/plans/{plan['id']}/book", json={}).json()
    assert started["target_date"] == plan["next_target_date"]
    assert started["wait_for_release"] is True
    wait_for(api, started["run_id"])
    assert seen["wait"] is True
    run = api.get(f"/runs/{started['run_id']}").json()
    assert run["status"] == "error" and run["detail"] == "RuntimeError: stop"
    assert api.get(f"/requests/{started['request_id']}").json()["status"] == "failed"


def test_payment_needs_both_the_operator_switch_and_the_caller(
    db, seeded, tmp_path, fake_booking
):
    settings = Settings(
        database_url=None,
        plan_id=None,
        submit_payment=True,
        headless=True,
        artifacts_dir=tmp_path,
        target_date_override=None,
    )
    worker = BookingWorker(db, settings, max_workers=1)
    seen = {}
    paid = BookingResult(tee_time=SLOT, submitted=True, confirmation="R1")
    fake_booking(paid, seen)
    with TestClient(create_app(settings, sessions=db, worker=worker)) as api:
        url = f"/plans/{seeded['soule_plan']}/book"
        # Caller opted out: dry run even though the operator switch is on.
        started = api.post(url, json={"target_date": "2026-10-03"}).json()
        assert started["dry_run"] is True
        worker.futures[uuid.UUID(started["run_id"])].result(timeout=10)
        assert seen["settings"].submit_payment is False

        started = api.post(
            url, json={"target_date": "2026-10-10", "submit_payment": True}
        ).json()
        assert started["dry_run"] is False
        worker.futures[uuid.UUID(started["run_id"])].result(timeout=10)
        assert seen["settings"].submit_payment is True
        run = api.get(f"/runs/{started['run_id']}").json()
        assert run["status"] == "success" and run["confirmation"] == "R1"
        request = api.get(f"/requests/{started['request_id']}").json()
        assert request["status"] == "booked"


def test_a_request_already_running_is_a_conflict(api, monkeypatch):
    release = threading.Event()

    def blocking_booking(*args, **kwargs):
        release.wait(timeout=10)
        raise RuntimeError("stop")

    monkeypatch.setattr(worker_module, "book_tee_time", blocking_booking)
    url = f"/plans/{api.ids['soule_plan']}/book"
    started = api.post(url, json={"target_date": "2026-10-03"})
    assert started.status_code == 202
    try:
        again = api.post(url, json={"target_date": "2026-10-03"})
        assert again.status_code == 409
        assert "running" in again.json()["detail"]
        run_id = started.json()["run_id"]
        assert api.get(f"/runs/{run_id}").json()["status"] == "running"
        # Nor can a running request be cancelled.
        cancel = api.post(f"/requests/{started.json()['request_id']}/cancel")
        assert cancel.status_code == 409
    finally:
        release.set()
        wait_for(api, started.json()["run_id"])


def test_a_failed_request_can_be_run_again_or_cancelled(api, fake_booking):
    fake_booking(RuntimeError("stop"))
    url = f"/plans/{api.ids['soule_plan']}/book"
    first = api.post(url, json={"target_date": "2026-10-03"}).json()
    wait_for(api, first["run_id"])
    request_id = first["request_id"]

    fake_booking(BookingResult(tee_time=SLOT, submitted=False))
    second = api.post(f"/requests/{request_id}/run", json={"wait_for_release": False})
    assert second.status_code == 202, second.text
    assert second.json()["request_id"] == request_id
    wait_for(api, second.json()["run_id"])

    runs = api.get(f"/requests/{request_id}/runs").json()
    assert [r["status"] for r in runs] == ["error", "dry_run"]
    pending = api.get("/requests", params={"status": "pending"}).json()
    assert [r["id"] for r in pending] == [request_id]

    cancelled = api.post(f"/requests/{request_id}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert api.post(f"/requests/{request_id}/run", json={}).status_code == 409


def test_a_one_off_request_needs_no_plan(api, fake_booking):
    seen = {}
    fake_booking(BookingResult(tee_time=SLOT, submitted=False), seen)
    created = api.post(
        "/requests",
        json={
            "user_email": "golfer@example.com",
            "course": "saticoy",
            "env_prefix": "GOLFNOW",
            "target_date": "2026-09-26",
            "earliest": "07:00",
            "latest": "09:00",
            "holes": 9,
            "players": 2,
        },
    )
    assert created.status_code == 201, created.text
    request = created.json()
    assert request["plan_id"] is None and request["status"] == "pending"

    started = api.post(f"/requests/{request['id']}/run", json={}).json()
    wait_for(api, started["run_id"])
    assert seen["course"].key == "saticoy" and seen["prefix"] == "GOLFNOW"
    assert seen["criteria"].target_date == date(2026, 9, 26)
    assert seen["criteria"].players == 2
    assert api.get("/runs").json()[0]["id"] == started["run_id"]


def test_unknown_ids_are_404(api):
    missing = uuid.uuid4()
    assert api.get(f"/plans/{missing}").status_code == 404
    assert api.post(f"/plans/{missing}/book", json={}).status_code == 404
    assert api.get(f"/requests/{missing}").status_code == 404
    assert api.post(f"/requests/{missing}/run", json={}).status_code == 404
    assert api.get(f"/runs/{missing}").status_code == 404


def test_health(api):
    assert api.get("/health").json() == {"status": "ok"}
