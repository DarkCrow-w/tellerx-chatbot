"""无需 Docker 的本地后端启动入口。"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import uvicorn
from alembic.config import Config

from alembic import command
from app.core.config import get_settings
from app.core.logging import configure_logging

logger = logging.getLogger(__name__)


def _project_root() -> Path:
    """定位包含 alembic.ini 的项目根目录并给出可操作的错误。"""

    current = Path.cwd()
    if (current / "alembic.ini").is_file():
        return current
    raise RuntimeError("请在项目根目录运行 tellerx-backend（未找到 alembic.ini）")


def _upgrade_database(project_root: Path) -> None:
    """启动前把目标 PostgreSQL 升级到当前代码所需结构。"""

    logger.info("开始执行数据库迁移 target=head")
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(project_root / "alembic"))
    # 本地启动器已经配置统一日志，阻止 Alembic 再次覆盖根 Logger。
    alembic_config.attributes["configure_logger"] = False
    command.upgrade(alembic_config, "head")
    logger.info("数据库迁移完成 target=head")


def _prepare_local_files() -> None:
    """在监听端口前验证本地目录、环境变量 Token 和模型清单。"""

    settings = get_settings()
    settings.storage_root.mkdir(parents=True, exist_ok=True)
    # 只验证环境变量存在，不输出或持久化 Token 内容。
    settings.require_model_api_key()
    if not settings.model_registry_path.is_file():
        raise RuntimeError(f"模型清单不存在: {settings.model_registry_path}")
    logger.info(
        "本地运行目录检查完成 storage_root=%s model_registry=%s",
        settings.storage_root,
        settings.model_registry_path,
    )


def main() -> None:
    """准备本地目录、执行迁移并启动 FastAPI。"""

    parser = argparse.ArgumentParser(description="启动 TellerX 本地后端")
    parser.add_argument("--host", default=os.getenv("TELLERX_API_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        default=int(os.getenv("TELLERX_API_PORT", "8000")),
        type=int,
    )
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--skip-migrations", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        project_root = _project_root()
        _prepare_local_files()
        if not args.skip_migrations:
            _upgrade_database(project_root)
    except Exception:
        logger.exception("后端启动准备失败")
        raise
    logger.info(
        "启动HTTP服务 host=%s port=%d reload=%s migrations=%s",
        args.host,
        args.port,
        args.reload,
        "skipped" if args.skip_migrations else "applied",
    )
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
