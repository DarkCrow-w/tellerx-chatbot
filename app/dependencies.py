from __future__ import annotations

from functools import lru_cache

from app.answering import AnswerService
from app.config import get_settings
from app.indexing import IndexingService
from app.ingestion import IngestionService
from app.model_router import ModelRegistry, QwenModelRouter
from app.parsers import DocumentParser
from app.qwen import QwenClient
from app.search import Retriever, SearchIndex


@lru_cache
def qwen_client() -> QwenClient:
    return QwenClient(get_settings())


@lru_cache
def search_index() -> SearchIndex:
    return SearchIndex(get_settings())


@lru_cache
def indexing_service() -> IndexingService:
    settings = get_settings()
    return IndexingService(settings, search_index())


@lru_cache
def model_registry() -> ModelRegistry:
    return ModelRegistry.load(get_settings().model_registry_path)


@lru_cache
def model_router() -> QwenModelRouter:
    return QwenModelRouter(model_registry(), qwen_client())


@lru_cache
def retriever() -> Retriever:
    return Retriever(get_settings(), search_index(), qwen_client())


@lru_cache
def answer_service() -> AnswerService:
    return AnswerService(get_settings(), retriever(), model_router())


@lru_cache
def ingestion_service() -> IngestionService:
    settings = get_settings()
    return IngestionService(
        settings,
        DocumentParser(settings.parser_backend),
        qwen_client(),
        search_index(),
        indexing_service(),
    )
