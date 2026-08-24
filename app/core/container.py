"""Application composition root.

Only this module wires concrete infrastructure adapters into application
services.  Business modules receive already-constructed collaborators, which
keeps object construction deterministic and prevents hidden clients from being
created in request handlers.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property, lru_cache

from app.core.config import Settings, get_settings
from app.integrations.qwen import QwenClient
from app.integrations.search import SearchIndex
from app.knowledge.parsers import DocumentParser
from app.services.answering import AnswerService
from app.services.indexing import IndexingService
from app.services.ingestion import IngestionService
from app.services.model_router import ModelRegistry, QwenModelRouter
from app.services.retrieval import Retriever


@dataclass
class ApplicationContainer:
    """Lazily construct one process-local instance of every shared service."""

    settings: Settings

    @cached_property
    def qwen(self) -> QwenClient:
        return QwenClient(self.settings)

    @cached_property
    def index(self) -> SearchIndex:
        return SearchIndex(self.settings)

    @cached_property
    def indexing(self) -> IndexingService:
        return IndexingService(self.settings, self.index)

    @cached_property
    def registry(self) -> ModelRegistry:
        return ModelRegistry.load(self.settings.model_registry_path)

    @cached_property
    def router(self) -> QwenModelRouter:
        return QwenModelRouter(self.registry, self.qwen)

    @cached_property
    def retrieval(self) -> Retriever:
        return Retriever(self.settings, self.index, self.qwen)

    @cached_property
    def answering(self) -> AnswerService:
        return AnswerService(self.settings, self.retrieval, self.router)

    @cached_property
    def ingestion(self) -> IngestionService:
        return IngestionService(
            self.settings,
            DocumentParser(self.settings.parser_backend),
            self.qwen,
            self.index,
            self.indexing,
        )

    def close(self) -> None:
        """Close only infrastructure clients that were actually initialized."""

        qwen = self.__dict__.get("qwen")
        if qwen is not None:
            qwen.close()
        index = self.__dict__.get("index")
        if index is not None:
            index.client.close()


@lru_cache
def application_container() -> ApplicationContainer:
    """Return the process-wide composition root.

    Worker, indexer, CLI, and API processes each get their own container.  No
    network client or database session is shared across process boundaries.
    """

    return ApplicationContainer(get_settings())


# Compatibility functions keep call sites concise and provide stable FastAPI
# dependency targets while all construction remains centralized above.
def qwen_client() -> QwenClient:
    return application_container().qwen


def search_index() -> SearchIndex:
    return application_container().index


def indexing_service() -> IndexingService:
    return application_container().indexing


def model_registry() -> ModelRegistry:
    return application_container().registry


def model_router() -> QwenModelRouter:
    return application_container().router


def retriever() -> Retriever:
    return application_container().retrieval


def answer_service() -> AnswerService:
    return application_container().answering


def ingestion_service() -> IngestionService:
    return application_container().ingestion
