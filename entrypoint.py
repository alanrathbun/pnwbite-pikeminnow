"""Entry point for Railway: APScheduler in background + HTTP server in main thread."""
from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

import scheduler
import fishing_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("entrypoint")

PACIFIC = ZoneInfo("America/Los_Angeles")


def main() -> None:
    sched = BackgroundScheduler(timezone=PACIFIC)
    scheduler.register_jobs(sched)
    sched.start()
    log.info("Scheduler started; jobs: %s", [j.id for j in sched.get_jobs()])

    scheduler.maybe_warmup()

    fishing_server.main()  # blocks


if __name__ == "__main__":
    main()
