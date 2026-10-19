"""Devices and sending a workout to one.

Garmin has no "put this on my watch now" call. A push adds a message to the
account's device message queue; the watch takes it at its next sync with
Garmin Connect (through the phone app, or Wi-Fi). So there are two separate
facts, reported separately:

- upload: did Garmin accept the push and queue it? (`verified` here means
  it's in the queue.)
- sync: has the watch fetched it? Garmin only shows the queue. While the
  message is there, the watch hasn't. Once it's gone, the watch has
  probably taken it, but Garmin also drops messages it gives up on, so
  that's `not_in_queue`, not "delivered". Only the watch's workout list
  confirms delivery.

Before a push, the device's own registration flags are checked
(`strengthWorkoutCapable`, `runningWorkoutCapable`, ...): a sport it says it
can't run is refused; one it says nothing about is a warning in the preview.
"""

from typing import Any

from garmin_mcp.errors import NotFound, Unsupported
from garmin_mcp.repositories.garmin_client import JSON, GarminRepository
from garmin_mcp.repositories.workout_codec import sport_key
from garmin_mcp.schemas.results import (
    ChangePreview,
    DevicePushResult,
    DeviceSummary,
    Verification,
    WriteResult,
)
from garmin_mcp.services.writes import Checked, WritePlan, WriteRunner

# Garmin's workout sport key -> the device registration flag saying the
# device can run that kind of workout (as seen on a Venu 4).
CAPABILITY_FLAGS = {
    "strength_training": "strengthWorkoutCapable",
    "running": "runningWorkoutCapable",
    "cycling": "cyclingWorkoutCapable",
    "cardio_training": "cardioWorkoutCapable",
    "hiit": "hiitWorkoutCapable",
    "yoga": "yogaWorkoutCapable",
    "pilates": "pilatesWorkoutCapable",
    "mobility": "mobilityWorkoutCapable",
}


class DeviceService:
    def __init__(
        self, repo: GarminRepository, runner: WriteRunner, default_device_id: int | None
    ) -> None:
        self._repo = repo
        self._runner = runner
        self._default_device_id = default_device_id

    def list_devices(self) -> list[DeviceSummary]:
        try:
            last_used = self._repo.get_last_used_device().get("userDeviceId")
        except NotFound:
            last_used = None
        return [
            _summary(d, last_used)
            for d in self._repo.list_devices()
            if d.get("deviceId")
        ]

    def push_status(self, workout_id: int, device_id: int) -> DevicePushResult:
        queued = bool(self._queued(workout_id, device_id))
        return DevicePushResult(
            upload_status="accepted" if queued else "unknown",
            sync_status="pending_on_garmin" if queued else "not_in_queue",
            device_id=device_id,
            workout_id=workout_id,
        )

    def push_workout(
        self,
        workout_id: int,
        device_id: int | None,
        *,
        dry_run: bool,
        confirmation_token: str | None,
    ) -> WriteResult:
        workout = self._repo.get_workout(workout_id)
        device = self._pick_device(device_id)
        target = int(device_id or device.device_id)
        name = workout.get("workoutName") or f"workout {workout_id}"
        warnings = self._check_capable(device, sport_key(workout))
        details = [
            "Garmin queues it; the watch picks it up at its next sync "
            "with Garmin Connect (phone app or Wi-Fi)."
        ]
        if device.max_workouts:
            details.append(
                f"The device holds up to {device.max_workouts} workouts; "
                f"Garmin doesn't say how many are on it now."
            )
        # Only a message that appears after the push proves the push: Garmin
        # queues a scheduled workout for the watch by itself, so one may be
        # waiting already.
        before = self._queued(workout_id, target)
        if before:
            warnings.append(
                "This workout is already waiting in Garmin's queue for this "
                "device (Garmin queues scheduled workouts itself); a push adds "
                "another copy."
            )

        def check() -> Checked:
            new = self._queued(workout_id, target) - before
            checked = "Garmin's device message queue"
            ids: dict[str, Any] = {
                "workout_id": workout_id,
                "device_id": target,
                "upload_status": "accepted" if new else "unknown",
                "sync_status": "pending_on_garmin" if new else "not_in_queue",
            }
            if new:
                return Verification(status="verified", checked=checked), ids
            return Verification(
                status="not_found",
                checked=checked,
                mismatches=[
                    "no new message for this workout in the queue: the watch "
                    "may already have taken it, or Garmin didn't queue it. "
                    "Check the watch's workout list after it syncs."
                ],
            ), ids

        plan = WritePlan(
            operation="push_workout_to_device",
            preview=ChangePreview(
                summary=(
                    f"Send {name!r} (workout {workout_id}) to "
                    f"{device.name or 'device'} ({target})"
                ),
                details=details,
                warnings=warnings,
            ),
            basis={"workout_id": workout_id, "device_id": target},
            execute=lambda: self._repo.push_workout_to_device(workout_id, target),
            verify=lambda _response: check(),
            reconcile=check,
            refs={"workout_id": workout_id, "device_id": target},
        )
        return self._runner.run(
            plan, dry_run=dry_run, confirmation_token=confirmation_token
        )

    def _pick_device(self, device_id: int | None) -> DeviceSummary:
        devices = self.list_devices()
        wanted = device_id or self._default_device_id
        if wanted is None:
            last = [d for d in devices if d.last_used]
            if not last:
                raise NotFound(
                    "device",
                    "Garmin doesn't report a last-used device; pass device_id "
                    "(see list_devices) or set GARMIN_DEFAULT_DEVICE_ID",
                )
            return last[0]
        for d in devices:
            if d.device_id == wanted:
                return d
        known = ", ".join(f"{d.device_id} ({d.name})" for d in devices) or "none"
        raise NotFound(
            "device",
            f"device {wanted} isn't registered to this account; known: {known}",
        )

    @staticmethod
    def _check_capable(device: DeviceSummary, sport: str | None) -> list[str]:
        """Refuse what the device says it can't run; warn when it doesn't say."""
        label = device.name or f"device {device.device_id}"
        if device.workout_sports.get("any") is False:
            raise Unsupported(f"{label} doesn't support workouts")
        can = device.workout_sports.get(sport or "")
        if can is False:
            raise Unsupported(f"{label} doesn't support {sport} workouts")
        if can is None:
            return [f"{label} doesn't say whether it supports {sport} workouts."]
        return []

    def _queued(self, workout_id: int, device_id: int) -> set[str]:
        """Ids of the queued messages delivering this workout to this device.

        As seen on a real account: `{"messageId", "deviceId", "messageType":
        "workouts", "messageStatus": "new", "metaData": {"metaDataId":
        <workout id>, "messageName", "messageUrl", ...}}`.
        """
        found = set()
        for m in _messages(self._repo.get_device_messages()):
            meta = m.get("metaData")
            if not isinstance(meta, dict):
                continue
            if (
                m.get("messageType") == "workouts"
                and _int(meta.get("metaDataId")) == workout_id
                and _int(m.get("deviceId")) == device_id
            ):
                found.add(str(m.get("messageId") or m.get("createdTimeStamp")))
        return found


def _summary(raw: JSON, last_used: Any) -> DeviceSummary:
    sports = {
        sport: raw[flag]
        for sport, flag in CAPABILITY_FLAGS.items()
        if isinstance(raw.get(flag), bool)
    }
    if isinstance(raw.get("workoutCapable"), bool):
        sports["any"] = raw["workoutCapable"]
    max_workouts = raw.get("maxWorkoutCount")
    return DeviceSummary(
        device_id=int(raw["deviceId"]),
        name=raw.get("displayName") or raw.get("productDisplayName"),
        model=raw.get("deviceTypeSimpleName") or raw.get("productDisplayName"),
        last_used=raw.get("deviceId") == last_used,
        workout_sports=sports,
        max_workouts=max_workouts if isinstance(max_workouts, int) else None,
    )


def _messages(raw: Any) -> list[JSON]:
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = raw.get("messages") or raw.get("deviceMessages") or []
    else:
        items = []
    return [m for m in items if isinstance(m, dict)]


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
