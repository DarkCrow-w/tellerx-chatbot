"""SQLAlchemy engine, declarative metadata, and request-scoped sessions."""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


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
    finally:
        session.close()
