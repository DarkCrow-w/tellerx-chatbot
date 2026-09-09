"""SQLAlchemy engine, declarative metadata, and request-scoped sessions."""

from __future__ import annotations

import logging
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """所有 ORM 模型共享的声明式元数据基类。"""


def _engine_kwargs(url: str) -> dict:
    """按数据库类型返回安全的连接池参数。"""

    if url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return {"pool_pre_ping": True, "pool_size": 10, "max_overflow": 20}


settings = get_settings()
engine = create_engine(settings.database_url, **_engine_kwargs(settings.database_url))
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """为一次请求提供数据库会话，并在请求结束后确保关闭。"""

    session = SessionLocal()
    try:
        yield session
    except Exception as exc:
        session.rollback()
        # HTTP 业务异常也会经过依赖清理，详细堆栈由请求中间件统一记录。
        logger.debug("数据库会话已回滚 error=%s", type(exc).__name__)
        raise
    finally:
        session.close()
