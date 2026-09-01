from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import health_router, router
from app.core.config import get_settings
from app.core.container import application_container, search_index

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """启动时验证搜索结构，退出时释放进程级基础设施资源。"""

    # 迁移负责创建 FTS/pgvector 结构；接收流量前验证扩展与索引，让不完整部署尽早失败。
    search_index().ensure_index()
    try:
        yield
    finally:
        # 滚动部署收到 SIGTERM 时主动释放连接池。
        application_container().close()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
app.include_router(health_router)
app.include_router(router, prefix="/api/v1")

static_dir = Path(__file__).parent / "static"
static_index = static_dir / "index.html"
if static_index.is_file():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", include_in_schema=False, response_model=None)
def index() -> FileResponse | RedirectResponse:
    """有构建产物时返回前端，否则引导到 API 文档。"""

    if static_index.is_file():
        return FileResponse(static_index)
    return RedirectResponse(url="/docs")
