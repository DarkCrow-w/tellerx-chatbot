from __future__ import annotations

import argparse
import json
import logging
import signal
import time

from app.config import get_settings
from app.db import SessionLocal
from app.dependencies import indexing_service

running = True


def _stop(*_: object) -> None:
    global running
    running = False


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    settings = get_settings()
    service = indexing_service()
    next_reconcile = time.monotonic()
    with SessionLocal() as db:
        service.recover_expired_leases(db)
    while running:
        if (
            settings.index_reconcile_interval_seconds > 0
            and time.monotonic() >= next_reconcile
        ):
            with SessionLocal() as db:
                try:
                    report = service.reconcile(db, repair=True)
                    logging.getLogger(__name__).info(
                        "Index reconciliation checked=%s differences=%s repaired=%s",
                        report["checked_versions"],
                        report["difference_count"],
                        report["repaired"],
                    )
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Scheduled index reconciliation failed"
                    )
            next_reconcile = (
                time.monotonic() + settings.index_reconcile_interval_seconds
            )
        with SessionLocal() as db:
            event_id = service.claim_next_event(db)
        if not event_id:
            time.sleep(settings.worker_poll_seconds)
            continue
        with SessionLocal() as db:
            service.publish_event(db, event_id)


def reconcile_main() -> None:
    parser = argparse.ArgumentParser(description="Compare PostgreSQL chunks with Elasticsearch")
    parser.add_argument("--repair", action="store_true")
    args = parser.parse_args()
    with SessionLocal() as db:
        report = indexing_service().reconcile(db, repair=args.repair)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["difference_count"] and not args.repair:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
