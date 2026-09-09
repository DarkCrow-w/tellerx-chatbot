"""Protect stage logs, error boundaries and model-call audit identities."""

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.core.config import Settings
from app.integrations.openai_client import ChatCallResult, ModelAPIError, OpenAIModelClient, Usage
from app.services.indexing import IndexingService
from app.services.ingestion import IngestionCancelled, IngestionService
from app.services.model_router import (
    ModelRegistry,
    NoModelAvailable,
    QwenModelRouter,
    RegisteredModel,
)
from app.services.retrieval import Retriever


def ingestion_service(**settings):
    service = IngestionService(
        Settings(_env_file=None, **settings),
        Mock(),
        Mock(),
        Mock(),
        indexer=Mock(),
        storage=Mock(),
    )
    service._ensure_embedding_model = Mock()
    service._load_embedding_cache = Mock(return_value={})
    return service


def chunks(count):
    return [SimpleNamespace(embedding_input_hash=str(index)) for index in range(count)]


def test_batches_log_progress_and_do_not_regenerate_cached_or_duplicate_inputs(caplog):
    caplog.set_level("INFO")
    service = ingestion_service()
    inputs = chunks(12)
    service._load_embedding_cache.return_value = {"0": object()}

    def save_batch(db, batch, fingerprint, cached):
        cached.update({chunk.embedding_input_hash: object() for chunk in batch})

    service._embed_batch = Mock(side_effect=save_batch)
    result = service._embeddings(Mock(), inputs + [inputs[1]], [], job_id="upload-job")
    assert len(result) == 12
    assert [len(call.args[1]) for call in service._embed_batch.call_args_list] == [10, 1]
    assert "chunks=13 unique_inputs=12 cached=1 missing=11" in caplog.text
    assert "batch=2/2 completed=11/11" in caplog.text
    assert "job_id=upload-job" in caplog.text
    assert "elapsed_ms=" in caplog.text


def test_all_cached_inputs_skip_embedding_calls(caplog):
    caplog.set_level("INFO")
    service = ingestion_service()
    service._load_embedding_cache.return_value = {"0": object()}
    service._embed_batch = Mock()
    service._embeddings(Mock(), chunks(1), [], job_id="cached-job")
    service._embed_batch.assert_not_called()
    assert "generated=0" in caplog.text


def test_provider_failure_keeps_partial_cache_and_logs_fallback(caplog):
    caplog.set_level("INFO")
    service = ingestion_service(allow_bm25_only=True)
    service._load_embedding_cache.return_value = {"0": object()}
    service._generate_missing_embeddings = Mock(
        side_effect=ModelAPIError("offline", status_code=503, code="unavailable")
    )
    db = Mock()
    warnings = []
    result = service._embeddings(db, chunks(2), warnings, job_id="failed-job")
    assert set(result) == {"0"}
    assert len(warnings) == 1
    db.rollback.assert_called_once()
    assert "cached=1 missing=1 status=503 code=unavailable" in caplog.text
    assert "Embedding阶段完成" not in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("bug"),
        OSError("disk full"),
        SQLAlchemyError("database failed"),
        ValueError("invalid local vector"),
        IngestionCancelled("deleted"),
    ],
)
def test_unexpected_failures_and_cancellation_are_never_bm25_fallback(error):
    service = ingestion_service(allow_bm25_only=True)
    service._generate_missing_embeddings = Mock(side_effect=error)
    warnings = []
    with pytest.raises(type(error)) as caught:
        service._embeddings(Mock(), chunks(1), warnings)
    assert caught.value is error
    assert not warnings


def test_provider_error_is_raised_when_fallback_is_disabled():
    service = ingestion_service(allow_bm25_only=False)
    service._generate_missing_embeddings = Mock(side_effect=ModelAPIError("offline"))
    with pytest.raises(ModelAPIError):
        service._embeddings(Mock(), chunks(1), [])


@pytest.mark.parametrize("winner_exists", [True, False])
def test_model_registration_only_suppresses_verified_concurrent_insert(winner_exists):
    service = ingestion_service()
    db = MagicMock()
    db.get.side_effect = [None, object() if winner_exists else None]
    error = IntegrityError("insert", {}, ValueError("constraint failed"))
    db.flush.side_effect = error
    if winner_exists:
        IngestionService._ensure_embedding_model(service, db)
        db.commit.assert_called_once()
    else:
        with pytest.raises(IntegrityError) as caught:
            IngestionService._ensure_embedding_model(service, db)
        assert caught.value is error
        db.commit.assert_not_called()


@pytest.mark.parametrize(
    "rows",
    [
        None,
        [],
        [SimpleNamespace(index=0, embedding=None)],
        [SimpleNamespace(index=1, embedding=[1.0, 2.0])],
        [SimpleNamespace(index=0, embedding=[1.0])],
        [SimpleNamespace(index=0, embedding=[float("nan"), 1.0])],
        [SimpleNamespace(index=0, embedding=[float("inf"), 1.0])],
        [SimpleNamespace(index=0, embedding=["text", 1.0])],
    ],
)
def test_malformed_embedding_response_raises_typed_error(rows):
    client = OpenAIModelClient.__new__(OpenAIModelClient)
    client.settings = SimpleNamespace(embedding_dimensions=2, embedding_model="test")
    client._client = Mock()
    client._client.embeddings.create.return_value = SimpleNamespace(data=rows)
    with pytest.raises(ModelAPIError) as caught:
        client.embeddings(["private document text"])
    assert caught.value.code == "invalid_response"


def test_embedding_response_reorders_rows_and_rejects_duplicate_indexes():
    client = OpenAIModelClient.__new__(OpenAIModelClient)
    client.settings = SimpleNamespace(embedding_dimensions=2)
    response = SimpleNamespace(
        data=[
            SimpleNamespace(index=1, embedding=[3.0, 4.0]),
            SimpleNamespace(index=0, embedding=[1.0, 2.0]),
        ]
    )
    assert client._embedding_vectors(response, 2) == [[1.0, 2.0], [3.0, 4.0]]
    response.data[0].index = 0
    with pytest.raises(ModelAPIError, match="indexes"):
        client._embedding_vectors(response, 2)


@pytest.mark.parametrize("error", [ModelAPIError("offline"), RuntimeError("bug")])
def test_query_embedding_only_falls_back_for_provider_errors(error):
    index = Mock()
    index.lexical_search.return_value = []
    retriever = Retriever(Settings(_env_file=None, allow_bm25_only=True), index, Mock())
    retriever._query_embedding = Mock(side_effect=error)
    if isinstance(error, ModelAPIError):
        assert retriever._retrieve_for_statuses("question", [], ["approved"]) == []
    else:
        with pytest.raises(RuntimeError) as caught:
            retriever._retrieve_for_statuses("question", [], ["approved"])
        assert caught.value is error


def model_router():
    model = RegisteredModel("test-model", "all", 1000, 1, True, True)
    router = QwenModelRouter(ModelRegistry([model]), Mock())
    router._record = Mock()
    return router


def test_successful_model_call_uses_provider_identity_without_unused_uuid(caplog):
    caplog.set_level("INFO")
    router = model_router()
    result = ChatCallResult("test-model", "provider-id", "{}", Usage(), 10.0)
    router.client.chat_json.return_value = result
    with patch("app.services.model_router.uuid.uuid4") as uuid4:
        assert (
            router.call(
                Mock(), tier="plus", system_prompt="", user_prompt="", pinned_model="test-model"
            )
            is result
        )
    uuid4.assert_not_called()
    assert router._record.call_args.kwargs["request_id"] == "provider-id"
    assert "provider-id" not in caplog.text


def test_failed_model_call_has_identity_cause_and_no_fictitious_fallback(caplog):
    caplog.set_level("INFO")
    router = model_router()
    error = ModelAPIError("offline", status_code=503, code="unavailable")
    router.client.chat_json.side_effect = error
    with (
        patch("app.services.model_router.uuid.uuid4", return_value="failure-id") as uuid4,
        pytest.raises(NoModelAvailable) as caught,
    ):
        router.call(
            Mock(), tier="plus", system_prompt="", user_prompt="", pinned_model="test-model"
        )
    uuid4.assert_called_once()
    assert caught.value.__cause__ is error
    assert router._record.call_args.kwargs["request_id"] == "failure-id"
    assert "next_model=none" in caplog.text


@pytest.mark.parametrize("attempts,expected", [(0, "pending"), (4, "dead")])
def test_index_failure_reports_retry_or_terminal_status(attempts, expected, caplog):
    caplog.set_level("INFO")
    indexer = IndexingService(Settings(_env_file=None), Mock(), Mock())
    event = SimpleNamespace(
        id="event",
        status="pending",
        attempts=attempts,
        event_type="index_version",
        aggregate_id="version",
        payload={},
    )
    db = Mock()
    db.get.return_value = event
    indexer._dispatch_event = Mock(side_effect=RuntimeError("index down"))
    assert indexer.publish_event(db, "event") is False
    assert event.status == expected
    assert f"status={expected}" in caplog.text
    assert f"retry_scheduled={expected == 'pending'}" in caplog.text
    assert "Traceback" in caplog.text
