"""Small supervised background drains for newly committed listing-analysis inputs."""

import logging
import signal
import threading

from . import config, db, listing_analysis
from .enrich import listing_queue


def main():
    stopped = threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stopped.set())
    signal.signal(signal.SIGINT,lambda *_:stopped.set())
    while not stopped.is_set():
        try:
            if config.LISTING_ANALYSIS_ENABLED and config.LLM_ENABLED and config.ENRICH_BUDGET > 0:
                listing_analysis.purge()
                due = db.scalar("SELECT COUNT(*) FROM enrichment_tasks WHERE task=? AND state='ready' AND due_at<=?",
                                (listing_analysis.TASK,listing_analysis.now().isoformat()))
                if due and "enrich" not in db.active_locks():
                    listing_queue.run(budget=min(10,config.ENRICH_BUDGET),concurrency=min(2,config.LLM_CONCURRENCY),runtime_seconds=60)
        except Exception as exc:
            logging.getLogger(__name__).warning("Automatic listing analysis deferred: %s",type(exc).__name__)
        stopped.wait(5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
