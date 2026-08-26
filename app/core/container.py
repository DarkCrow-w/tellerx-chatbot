"""Application composition root.

Only this module wires concrete infrastructure adapters into application
services.  Business modules receive already-constructed collaborators, which
keeps object construction deterministic and prevents hidden clients from being
created in request handlers.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property, lru_cache

from app.application.chat_service import ChatApplicationService
from app.application.document_service import DocumentApplicationService
from app.application.operations_service import (
    HealthApplicationService,
    OperationsApplicationService,
)
from app.core.config import Settings, get_settings
from app.integrations.qwen import QwenClient
from app.integrations.search import SearchIndex
from app.integrations.storage import LocalObjectStorage
from app.knowledge.parsers import DocumentParser
from app.repositories.chat import ChatRepository
from app.repositories.documents import DocumentRepository
from app.repositories.operations import OperationsRepository
from app.services.answering import AnswerService
from app.services.indexing import IndexingService
from app.services.ingestion import IngestionService
from app.services.model_router import ModelRegistry, QwenModelRouter
from app.services.query_understanding import QueryUnderstandingService
from app.services.retrieval import Retriever


@dataclass
class ApplicationContainer:
    """惰性构造每项共享服务在当前进程内的唯一实例。"""

    settings: Settings

    @cached_property
    def qwen(self) -> QwenClient:
        """返回复用连接池的千问客户端。"""

        return QwenClient(self.settings)

    @cached_property
    def index(self) -> SearchIndex:
        """返回 PostgreSQL 搜索适配器。"""

        return SearchIndex(self.settings)

    @cached_property
    def storage(self) -> LocalObjectStorage:
        """返回进程内共享的不可变对象存储适配器。"""

        return LocalObjectStorage(self.settings.storage_root)

    @cached_property
    def chat_repository(self) -> ChatRepository:
        """返回无状态问答 Repository。"""

        return ChatRepository()

    @cached_property
    def document_repository(self) -> DocumentRepository:
        """返回无状态文档 Repository。"""

        return DocumentRepository()

    @cached_property
    def operations_repository(self) -> OperationsRepository:
        """返回无状态运维 Repository。"""

        return OperationsRepository()

    @cached_property
    def indexing(self) -> IndexingService:
        """返回搜索投影发布与校验服务。"""

        return IndexingService(self.settings, self.index, self.storage)

    @cached_property
    def registry(self) -> ModelRegistry:
        """首次访问时从配置加载模型注册表。"""

        return ModelRegistry.load(self.settings.model_registry_path)

    @cached_property
    def router(self) -> QwenModelRouter:
        """返回带配额与故障转移策略的模型路由器。"""

        return QwenModelRouter(self.registry, self.qwen)

    @cached_property
    def retrieval(self) -> Retriever:
        """返回混合检索服务。"""

        return Retriever(self.settings, self.index, self.qwen)

    @cached_property
    def query_understanding(self) -> QueryUnderstandingService:
        """返回语义查询理解服务。"""

        return QueryUnderstandingService(self.settings, self.router)

    @cached_property
    def answering(self) -> AnswerService:
        """返回完整的证据约束回答服务。"""

        return AnswerService(
            self.settings,
            self.retrieval,
            self.router,
            self.query_understanding,
            self.chat_repository,
        )

    @cached_property
    def ingestion(self) -> IngestionService:
        """返回文档解析、切块和向量化服务。"""

        return IngestionService(
            self.settings,
            DocumentParser(self.settings.parser_backend),
            self.qwen,
            self.index,
            self.indexing,
            self.storage,
        )

    @cached_property
    def chat_application(self) -> ChatApplicationService:
        """返回问答与反馈应用服务。"""

        return ChatApplicationService(lambda: self.answering, self.chat_repository)

    @cached_property
    def document_application(self) -> DocumentApplicationService:
        """返回文档目录与生命周期应用服务。"""

        return DocumentApplicationService(
            self.settings,
            self.storage,
            self.document_repository,
            self.ingestion,
        )

    @cached_property
    def operations_application(self) -> OperationsApplicationService:
        """返回运行状态与维护应用服务。"""

        return OperationsApplicationService(
            self.settings,
            self.operations_repository,
            self.index,
            self.indexing,
            self.router,
        )

    @cached_property
    def health_application(self) -> HealthApplicationService:
        """返回依赖就绪检查应用服务。"""

        return HealthApplicationService(self.operations_repository, self.index)

    def close(self) -> None:
        """只关闭实际初始化过的基础设施客户端。"""

        qwen = self.__dict__.get("qwen")
        if qwen is not None:
            qwen.close()
        index = self.__dict__.get("index")
        if index is not None:
            index.close()


@lru_cache
def application_container() -> ApplicationContainer:
    """返回当前进程的组合根。

    Worker、索引器、CLI 和 API 各自拥有容器，不跨进程共享网络客户端或数据库会话。
    """

    return ApplicationContainer(get_settings())


# 这些薄函数让调用点保持简洁，也为 FastAPI 提供稳定依赖目标；对象构造仍集中在上方。
def qwen_client() -> QwenClient:
    """获取进程级千问客户端。"""

    return application_container().qwen


def search_index() -> SearchIndex:
    """获取进程级搜索适配器。"""

    return application_container().index


def indexing_service() -> IndexingService:
    """获取索引发布服务。"""

    return application_container().indexing


def model_registry() -> ModelRegistry:
    """获取模型注册表。"""

    return application_container().registry


def model_router() -> QwenModelRouter:
    """获取模型路由器。"""

    return application_container().router


def retriever() -> Retriever:
    """获取混合检索服务。"""

    return application_container().retrieval


def answer_service() -> AnswerService:
    """获取证据约束回答服务。"""

    return application_container().answering


def ingestion_service() -> IngestionService:
    """获取文档入库服务。"""

    return application_container().ingestion


def chat_application_service() -> ChatApplicationService:
    """获取问答与反馈应用服务。"""

    return application_container().chat_application


def document_application_service() -> DocumentApplicationService:
    """获取文档应用服务。"""

    return application_container().document_application


def operations_application_service() -> OperationsApplicationService:
    """获取运维应用服务。"""

    return application_container().operations_application


def health_application_service() -> HealthApplicationService:
    """获取就绪检查应用服务。"""

    return application_container().health_application
