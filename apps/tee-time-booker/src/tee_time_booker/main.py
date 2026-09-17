from datetime import datetime, timezone

from loguru import logger

from tee_time_booker import config
from tee_time_booker.booking import PaymentOutcomeUnknownError, book_tee_time
from tee_time_booker.config import Credentials, Settings
from tee_time_booker.dates import target_date
from tee_time_booker.run_log import RunRecord, log_run


def main() -> int:
    """Book one tee time, append the outcome to the CSV log, return an exit code."""
    settings = Settings.from_env()
    created_at = datetime.now(timezone.utc)
    run_id = created_at.strftime("%Y%m%dT%H%M%SZ")

    target = settings.target_date_override or target_date()
    logger.info(
        f"Run {run_id}: booking {config.COURSE_NAME} on {target:%A %Y-%m-%d} "
        f"between {config.EARLIEST_TEE_TIME:%I:%M %p} "
        f"and {config.LATEST_TEE_TIME:%I:%M %p}"
    )

    tee_time = None
    status = "Failed"
    try:
        creds = Credentials.from_env()
        tee_time = book_tee_time(target, creds, settings, run_id)
        status = "Success" if settings.submit_payment else "Success (dry run)"
        logger.success(f"{status}: {tee_time.start:%A %Y-%m-%d %I:%M %p}")
    except PaymentOutcomeUnknownError as e:
        status = f"Unknown: {e}"
        logger.exception(status)
    except Exception as e:
        # log.csv is committed to the repo, so keep only a short message there
        # (error messages can embed server responses); the traceback goes to
        # the job log.
        status = f"Error in booking process: {_brief(e)}"
        logger.exception(status)
    finally:
        log_run(
            RunRecord(
                run_id=run_id,
                created_at=created_at,
                finished_at=datetime.now(timezone.utc),
                course_name=config.COURSE_NAME if tee_time else None,
                tee_time_pst=tee_time.start if tee_time else None,
                cost_dollars=tee_time.green_fee if tee_time else None,
                service_fee_dollars=None,
                status=status,
            ),
            settings.log_path,
        )

    return 0 if status.startswith("Success") else 1


def _brief(e: Exception, limit: int = 200) -> str:
    """`ExcType: first line of the message`, capped at `limit` characters."""
    message = str(e).splitlines()[0] if str(e) else ""
    text = f"{type(e).__name__}: {message}" if message else type(e).__name__
    return text[:limit]


if __name__ == "__main__":
    raise SystemExit(main())
