"""Standalone reminder worker.

Run with ``python -m reminder_worker``. It is intentionally separate from the
FastAPI process so every backend replica does not run its own scheduler.
"""

from __future__ import annotations

import logging
import os
import signal
import threading

from services.notification_service import ReminderWorker, ReminderWorkerConfig

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    config = ReminderWorkerConfig.from_env()
    worker = ReminderWorker(config=config)
    stopping = threading.Event()

    def stop(signum, _frame) -> None:
        logger.info("Received signal %s; stopping reminder worker.", signum)
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    once = os.getenv("REMINDER_WORKER_ONCE", "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }

    logger.info(
        "Reminder worker started (poll=%ss, batch=%s).",
        config.poll_seconds,
        config.batch_size,
    )

    while not stopping.is_set():
        try:
            stats = worker.run_once()
            logger.info("Reminder worker cycle: %s", stats)
        except Exception:
            logger.exception("Reminder worker cycle failed; continuing.")

        if once:
            break

        stopping.wait(config.poll_seconds)

    logger.info("Reminder worker stopped.")


if __name__ == "__main__":
    main()
