"""APScheduler job registration for the pikeminnow report.

One job: daily report at 05:30 Pacific.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger("scheduler")


def register_jobs(sched) -> None:
    sched.add_job(_run_daily, CronTrigger(hour=5, minute=30), id="daily_report")


def _run_daily() -> None:
    log.info("Running pikeminnow daily report job")
    from fishing_report import main as run_report
    run_report()
    # Pikeminnow has no midday refresh; cache purge is optional.
    # The 24h Cloudflare TTL plus daily cron is sufficient for pikeminnow's
    # cadence. Phase 1 leaves cache purge off for pikeminnow.


def maybe_warmup() -> None:
    """Run the daily job once if no report.html exists in DATA_DIR."""
    data_dir = Path(os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent)))
    report = data_dir / "report.html"
    if not report.exists():
        log.info("No report at %s; running warmup daily job", report)
        _run_daily()
    else:
        log.info("Report exists at %s; skipping warmup", report)
