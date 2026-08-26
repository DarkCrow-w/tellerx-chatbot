"""运行状态、模型用量和索引维护用例。"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.repositories.operations import OperationsRepository


class SearchStatusPort(Protocol):
    """搜索后端健康状态端口。"""

    def status(self) -> dict: ...


class IndexMaintenancePort(Protocol):
    """搜索投影一致性维护端口。"""

    def reconcile(self, db: Session, *, repair: bool = False) -> dict: ...


class ModelUsagePort(Protocol):
    """模型用量查询端口。"""

    def usage_rows(self, db: Session) -> list[dict]: ...


class OperationsApplicationService:
    """编排只读运维查询和显式索引修复。"""

    def __init__(
        self,
        settings: Settings,
        repository: OperationsRepository,
        search: SearchStatusPort,
        indexing: IndexMaintenancePort,
        model_router: ModelUsagePort,
    ):
        self.settings = settings
        self.repository = repository
        self.search = search
        self.indexing = indexing
        self.model_router = model_router

    def index_status(self, db: Session) -> dict:
        """合并搜索后端状态与数据库一致性计数。"""

        state = self.search.status()
        state["embedding_fingerprint"] = self.settings.embedding_fingerprint
        state.update(
            self.repository.index_counters(db, self.settings.embedding_fingerprint)
        )
        return state

    def reconcile_index(self, db: Session, *, repair: bool) -> dict:
        """比较事实表和搜索投影，并按调用参数决定是否修复。"""

        return self.indexing.reconcile(db, repair=repair)

    def model_usage(self, db: Session) -> list[dict]:
        """返回模型路由器记录的本地用量估算。"""

        return self.model_router.usage_rows(db)


class HealthApplicationService:
    """组合数据库与搜索后端的就绪状态。"""

    def __init__(self, repository: OperationsRepository, search: SearchStatusPort):
        self.repository = repository
        self.search = search

    def readiness(self, db: Session) -> dict[str, str | bool]:
        """返回可供编排器判断的依赖就绪信息。"""

        database = self.repository.database_available(db)
        search_state = self.search.status()
        search = bool(search_state.get("available") and search_state.get("table_ready"))
        return {
            "status": "ready" if database and search else "not_ready",
            "database": database,
            "search": search,
        }
