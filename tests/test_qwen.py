import json

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.integrations.openai_client import ModelAPIError, OpenAIModelClient, parse_json_object


def settings(tmp_path):
    key = tmp_path / "key"
    key.write_text("fake-secret", encoding="utf-8")
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        model_api_key_file=key,
        model_api_base_url="https://model-api.test/v1",
        model_api_max_retries=0,
        embedding_dimensions=2,
    )


def test_openai_sdk_client_keeps_key_out_of_embedding_payload(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer fake-secret"
        body = json.loads(request.content)
        assert "fake-secret" not in json.dumps(body)
        if request.url.path.endswith("/embeddings"):
            assert body["model"] == "qwen3-embedding"
            assert body["dimensions"] == 2
            assert body["encoding_format"] == "float"
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}], "usage": {"total_tokens": 2}})
        raise AssertionError(request.url)

    client = OpenAIModelClient(settings(tmp_path), transport=httpx.MockTransport(handler))
    vectors, usage = client.embeddings(["hello"])
    assert vectors == [[0.1, 0.2]]
    assert usage.total_tokens == 2


def test_chat_json_uses_deterministic_structured_generation(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["temperature"] == 0.0
        assert body["response_format"] == {"type": "json_object"}
        assert "enable_thinking" not in body
        return httpx.Response(
            200,
            json={
                "id": "request-1",
                "model": "test-model",
                "choices": [{"message": {"content": '{"status":"ok"}'}}],
            },
        )

    client = OpenAIModelClient(settings(tmp_path), transport=httpx.MockTransport(handler))
    result = client.chat_json(
        model_id="test-model",
        system_prompt="system",
        user_prompt="user",
    )

    assert result.content == '{"status":"ok"}'


def test_rerank_is_disabled_without_sending_an_http_request(tmp_path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("disabled rerank must not send an HTTP request")

    client = OpenAIModelClient(settings(tmp_path), transport=httpx.MockTransport(handler))
    with pytest.raises(ModelAPIError, match="disabled"):
        client.rerank("question", ["candidate"], 1)


def test_default_embedding_model_uses_a_model_specific_index() -> None:
    configured = Settings(_env_file=None)

    assert configured.embedding_model == "qwen3-embedding"
    assert configured.embedding_dimensions == 1024
    assert configured.search_index_name.startswith("postgresql-chunk_search_index-s1-e")
    assert configured.search_index_name.endswith("-000001")
    assert configured.embedding_fingerprint in configured.search_index_name


def test_legacy_qwen_environment_names_remain_compatible(tmp_path) -> None:
    key = tmp_path / "legacy-key"
    key.write_text("legacy-secret", encoding="utf-8")
    configured = Settings(
        _env_file=None,
        qwen_api_key_file=key,
        qwen_chat_base_url="https://legacy.example/v1",
        qwen_embedding_model="legacy-embedding",
        qwen_embedding_dimensions=3,
        database_url="sqlite+pysqlite:///:memory:",
    )

    assert configured.model_api_key_file == key
    assert configured.model_api_base_url == "https://legacy.example/v1"
    assert configured.embedding_model == "legacy-embedding"
    assert configured.embedding_dimensions == 3


def test_postgres_vector_dimension_mismatch_fails_during_configuration() -> None:
    with pytest.raises(ValidationError, match="requires a new PostgreSQL vector schema"):
        Settings(
            _env_file=None,
            database_url="postgresql+psycopg://knowledge:knowledge@postgres/knowledge",
            embedding_dimensions=768,
        )


def test_parse_json_fence() -> None:
    assert parse_json_object('```json\n{"status":"ok"}\n```') == {"status": "ok"}
