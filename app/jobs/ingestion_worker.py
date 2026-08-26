"""Long-running document ingestion worker entry point."""

from __future__ import annotations

import logging
import signal
import time

from app.core.config import get_settings
from app.core.container import application_container, ingestion_service
from app.db import SessionLocal

logger = logging.getLogger(__name__)
running = True


def _stop(*_: object) -> None:
    """收到终止信号后停止领取新任务，让当前循环自然退出。"""

    global running
    running = False


def main() -> None:
    """持续领取并执行入库任务，每个阶段使用独立数据库会话。"""

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    settings = get_settings()
    service = ingestion_service()
    logger.info("Knowledge ingestion worker started")
    while running:
        job_id = None
        with SessionLocal() as db:
            try:
                job_id = service.claim_next_job(db)
            except Exception:
                logger.exception("Could not claim ingestion job")
        if not job_id:
            time.sleep(settings.worker_poll_seconds)
            continue
        with SessionLocal() as db:
            try:
                service.process(db, job_id)
            except Exception as exc:  # 服务层在返回前已持久化完整错误，这里只记录摘要。
                logger.warning("Ingestion job %s ended with %s", job_id, type(exc).__name__)
    application_container().close()


if __name__ == "__main__":
    main()
