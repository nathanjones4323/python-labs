"""The one path every write takes: validate, preview, approve, execute once,
read back, compare, log.

A service builds a `WritePlan` (validation, current-state reads and conflict
checks happen while building it, and raise before anything is previewed),
then hands it to `WriteRunner.run`, which owns the rest:

1. Dry run (the call asked for one, or `GARMIN_DRY_RUN` is on): return the
   preview. Nothing is sent.
2. No confirmation token: return the preview and a token.
3. Token: check it matches this exact plan, then call `execute` once.
4. Garmin definitely refused (`WriteRejected`): `failed`.
5. The outcome is unclear (`WriteOutcomeUnknown`): don't retry; run the
   plan's read-only `reconcile` to see whether it landed. `verified` if it
   did, `unknown` otherwise.
6. Otherwise `verify` reads the result back and compares it with what was
   intended: `verified`, or `unknown` with what differed.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from garmin_mcp.approval import ApprovalBook, fingerprint
from garmin_mcp.errors import GarminMcpError, WriteOutcomeUnknown, WriteRejected
from garmin_mcp.oplog import OperationLog
from garmin_mcp.schemas.results import (
    ChangePreview,
    Verification,
    WriteResult,
    WriteStatus,
)

log = logging.getLogger(__name__)

# A verification: what was found, and the ids to report.
Checked = tuple[Verification, dict[str, Any]]


@dataclass
class WritePlan:
    operation: str
    preview: ChangePreview
    # Everything the write depends on: the payload and the Garmin state it
    # was planned against. The confirmation token is bound to it.
    basis: Any
    execute: Callable[[], Any]
    verify: Callable[[Any], Checked]
    reconcile: Callable[[], Checked]
    refs: dict[str, Any] = field(default_factory=dict)


class WriteRunner:
    def __init__(
        self, *, dry_run: bool, approvals: ApprovalBook, oplog: OperationLog
    ) -> None:
        self.dry_run = dry_run
        self._approvals = approvals
        self._oplog = oplog

    def run(
        self, plan: WritePlan, *, dry_run: bool, confirmation_token: str | None
    ) -> WriteResult:
        op = plan.operation
        if dry_run or self.dry_run:
            why = (
                "GARMIN_DRY_RUN is on, so writes only preview. Set "
                "GARMIN_DRY_RUN=false and restart the server to allow changes."
                if self.dry_run
                else "Dry run: nothing was sent to Garmin."
            )
            self._oplog.record(op, "dry_run", plan.refs)
            return WriteResult(
                operation=op, status="dry_run", message=why, preview=plan.preview
            )

        basis = fingerprint(op, plan.basis)
        if confirmation_token is None:
            token, expires = self._approvals.issue(op, basis)
            return WriteResult(
                operation=op,
                status="approval_required",
                message=(
                    "Nothing was sent. Show this preview to the user. Only if "
                    "they approve, call the same tool again with the same "
                    "arguments and this confirmation_token."
                ),
                preview=plan.preview,
                confirmation_token=token,
                token_expires_at=expires,
            )

        # Raises ApprovalInvalid (a tool error) if the token doesn't fit.
        self._approvals.consume(confirmation_token, op, basis)

        try:
            response = plan.execute()
        except WriteRejected as exc:
            self._oplog.record(op, "failed", plan.refs, exc.code)
            return WriteResult(
                operation=op,
                status="failed",
                message="Garmin didn't apply the change.",
                preview=plan.preview,
                error=str(exc),
            )
        except WriteOutcomeUnknown as exc:
            log.warning("%s: outcome unknown (%s); reconciling", op, exc)
            return self._reconcile(plan, exc)
        except GarminMcpError as exc:
            # The repository only raises the two above for the write itself;
            # anything else came from a read before it, so nothing was sent.
            self._oplog.record(op, "failed", plan.refs, exc.code)
            return WriteResult(
                operation=op,
                status="failed",
                message="Nothing was sent: a read before the write failed.",
                preview=plan.preview,
                error=f"{exc.code}: {exc}",
            )

        verification, ids = self._checked(lambda: plan.verify(response))
        status: WriteStatus = (
            "verified" if verification.status == "verified" else "unknown"
        )
        self._oplog.record(op, status, plan.refs | ids)
        return WriteResult(
            operation=op,
            status=status,
            message=(
                "Done, and the read-back matches."
                if status == "verified"
                else "Garmin accepted the request, but the read-back doesn't "
                "match what was intended. Don't retry; look at the "
                "verification and check Garmin Connect."
            ),
            preview=plan.preview,
            result=ids or None,
            verification=verification,
        )

    def _reconcile(self, plan: WritePlan, exc: WriteOutcomeUnknown) -> WriteResult:
        verification, ids = self._checked(plan.reconcile)
        landed = verification.status == "verified"
        status: WriteStatus = "verified" if landed else "unknown"
        self._oplog.record(plan.operation, status, plan.refs | ids, exc.code)
        return WriteResult(
            operation=plan.operation,
            status=status,
            message=(
                "The request errored, but reading back shows the change was applied."
                if landed
                else "It's unclear whether Garmin applied the change, and the "
                "read-back couldn't confirm it either way. Do NOT retry: "
                "check Garmin Connect (or the read tools) first."
            ),
            preview=plan.preview,
            result=ids or None,
            verification=verification,
            error=str(exc),
        )

    @staticmethod
    def _checked(check: Callable[[], Checked]) -> Checked:
        # The write was already sent: whatever goes wrong reading it back must
        # still end in a result (and a log line), never a bare tool error that
        # invites a retry.
        try:
            return check()
        except GarminMcpError as exc:
            reason = f"{exc.code}: {exc}"
        except Exception as exc:
            log.exception("read-back crashed")
            reason = f"unexpected {type(exc).__name__} reading back"
        return (
            Verification(
                status="unreadable",
                checked="the read-back failed",
                mismatches=[reason],
            ),
            {},
        )
