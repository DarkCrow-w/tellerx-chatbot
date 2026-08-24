import json

import httpx

from app.core.config import Settings
from app.integrations.qwen import QwenClient, parse_json_object


def settings(tmp_path):
    key = tmp_path / "key"
    key.write_text("fake-secret", encoding="utf-8")
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        qwen_api_key_file=key,
        qwen_chat_base_url="https://qwen.test/v1",
        qwen_rerank_base_url="https://qwen.test/rerank/v1",
        qwen_max_retries=0,
    )


def test_qwen_client_does_not_require_key_in_payload(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer fake-secret"
        body = json.loads(request.content)
        assert "fake-secret" not in json.dumps(body)
        if request.url.path.endswith("/embeddings"):
            assert body["model"] == "qwen3.7-text-embedding"
            assert body["dimensions"] == 1024
            assert body["encoding_format"] == "float"
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}], "usage": {"total_tokens": 2}})
        raise AssertionError(request.url)

    client = QwenClient(settings(tmp_path), transport=httpx.MockTransport(handler))
    vectors, usage = client.embeddings(["hello"])
    assert vectors == [[0.1, 0.2]]
    assert usage.total_tokens == 2


def test_default_embedding_model_uses_a_model_specific_index() -> None:
    configured = Settings(_env_file=None)

    assert configured.qwen_embedding_model == "qwen3.7-text-embedding"
    assert configured.qwen_embedding_dimensions == 1024
    assert configured.search_index_name.startswith("knowledge-chunks-s3-e")
    assert configured.search_index_name.endswith("-000001")
    assert configured.embedding_fingerprint in configured.search_index_name


def test_parse_json_fence() -> None:
    assert parse_json_object('```json\n{"status":"ok"}\n```') == {"status": "ok"}
