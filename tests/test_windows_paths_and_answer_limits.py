"""Regression coverage for long storage names and truncated model responses."""

import errno
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.config import Settings
from app.integrations.openai_client import ModelAPIError, OpenAIModelClient, parse_json_object
from app.integrations.storage import LocalObjectStorage, StoragePathTooLongError
from app.knowledge.evidence import Evidence
from app.services.answer_contract import AnswerGenerationError
from app.services.answering import AnswerPreparation, AnswerService
from app.services.model_router import ModelRegistry, QwenModelRouter, RegisteredModel
from app.services.query_understanding import fallback_query_plan


@pytest.mark.parametrize("filename", ["a" * 220 + ".md", "文" * 100 + ".docx", "文" * 300 + ".PDF"])
def test_long_upload_filename_uses_short_parseable_storage_path(tmp_path, filename):
    storage = LocalObjectStorage(tmp_path)
    path, _, size = storage.save(BytesIO(b"document content"), filename, 1024)
    assert path.read_bytes() == b"document content"
    assert size == 16
    assert path.suffix == filename[filename.rindex(".") :].lower()
    assert len(path.name.encode("utf-8")) <= 100


def chat_client(content, finish_reason):
    client = OpenAIModelClient.__new__(OpenAIModelClient)
    client.settings = SimpleNamespace(model_api_json_mode_enabled=True, answer_max_tokens=8192)
    client._client = Mock()
    client._client.chat.completions.create.return_value = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)
        ],
        model="test-model",
        id="provider-id",
        usage=None,
    )
    return client


@pytest.mark.parametrize("content", ['{"answer":"' + "很长的回答" * 1000, "", "{}"])
def test_truncated_json_is_reported_as_output_limit_before_json_decode(content):
    client = chat_client(content, "length")
    with pytest.raises(ModelAPIError) as caught:
        result = client.chat_json(model_id="test-model", system_prompt="", user_prompt="")
        parse_json_object(result.content)
    assert caught.value.code == "output_truncated"


@pytest.mark.parametrize("windows", [False, True])
def test_storage_reports_path_limit_with_original_cause(tmp_path, monkeypatch, windows):
    storage = LocalObjectStorage(tmp_path)
    failure = OSError(errno.ENAMETOOLONG, "too long")
    if windows:
        failure = OSError("Windows path too long")
        failure.winerror = 206
    monkeypatch.setattr(Path, "open", Mock(side_effect=failure))
    with pytest.raises(StoragePathTooLongError, match="STORAGE_ROOT") as caught:
        storage.save(BytesIO(b"content"), "short.md", 1024)
    assert caught.value.__cause__ is failure


def test_storage_preserves_other_errors(tmp_path, monkeypatch):
    storage = LocalObjectStorage(tmp_path)
    failure = PermissionError("denied")
    monkeypatch.setattr(Path, "open", Mock(side_effect=failure))
    with pytest.raises(PermissionError) as caught:
        storage.save(BytesIO(b"content"), "short.md", 1024)
    assert caught.value is failure


def test_long_artifact_name_keeps_immutable_write_semantics(tmp_path):
    storage = LocalObjectStorage(tmp_path)
    name = "a" * 220 + ".json"
    first = storage.save_bytes(name, b"content")
    assert storage.save_bytes(name, b"content") == first
    with pytest.raises(ValueError, match="Immutable object collision"):
        storage.save_bytes(name, b"different")
    assert (tmp_path / name).read_bytes() == b"content"
    assert list(tmp_path.iterdir()) == [tmp_path / name]


def generation_service(responses):
    client = chat_client("", "stop")
    client._client.chat.completions.create.side_effect = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=reason)
            ],
            model="test-model",
            id="provider-id",
            usage=None,
        )
        for content, reason in responses
    ]
    registry = ModelRegistry([RegisteredModel("test-model", "all", 1000000, 1, True, True)])
    router = QwenModelRouter(registry, client)
    router._record = Mock()
    service = AnswerService(
        Settings(_env_file=None, validate_citations_against_database=False), Mock(), router
    )
    evidence = Evidence(
        "chunk", "doc", "version", "project", "source.md", "approved", "text-document", "原文证据"
    )
    preparation = AnswerPreparation(
        fallback_query_plan("问题", "test"), [evidence], "plus", "prompt"
    )
    return service, preparation, client


@pytest.mark.parametrize("first_reason", ["length", "stop"])
def test_truncated_or_malformed_answer_retries_with_larger_budget(first_reason):
    answer = "有证据支持的详细解释。" * 1000
    valid = json.dumps(
        {
            "status": "answered",
            "answer": answer,
            "claims": [{"text": answer, "evidence": [{"id": "chunk", "quote": "原文证据"}]}],
        },
        ensure_ascii=False,
    )
    service, preparation, client = generation_service(
        [('{"answer":"unfinished', first_reason), (valid, "stop")]
    )
    result = service._generate_answer(
        Mock(), question="问题", preparation=preparation, pinned_model="test-model"
    )
    assert result.validated.answer == answer
    calls = client._client.chat.completions.create.call_args_list
    assert [call.kwargs["max_tokens"] for call in calls] == [8192, 16384]
    assert [call.kwargs["model"] for call in calls] == ["test-model", "test-model"]
    audit = service.router._record.call_args_list
    if first_reason == "length":
        assert audit[0].kwargs["status"] == "failed"
        assert audit[0].kwargs["error_code"] == "output_truncated"
        assert audit[0].kwargs["request_id"] == "provider-id"
    assert audit[-1].kwargs["status"] == "success"


@pytest.mark.parametrize(
    "reason,code", [("length", "answer_output_truncated"), ("stop", "answer_invalid_json")]
)
def test_retry_exhaustion_reports_specific_error(reason, code):
    service, preparation, client = generation_service([('{"answer":"unfinished', reason)] * 2)
    result = service._generate_answer(
        Mock(), question="问题", preparation=preparation, pinned_model="test-model"
    )
    with pytest.raises(AnswerGenerationError) as caught:
        service._generation_refusal("问题", result)
    assert caught.value.code == code
    assert client._client.chat.completions.create.call_count == 2


def test_explicit_query_budget_is_not_increased():
    client = chat_client("{}", "stop")
    client.chat_json(model_id="test-model", system_prompt="", user_prompt="", max_tokens=700)
    assert client._client.chat.completions.create.call_args.kwargs["max_tokens"] == 700


def test_answer_budget_is_configurable_and_validated():
    settings = Settings(_env_file=None, answer_max_tokens=4096, answer_retry_max_tokens=12000)
    assert settings.answer_max_tokens == 4096
    assert settings.answer_retry_max_tokens == 12000
    with pytest.raises(ValueError, match="ANSWER_RETRY_MAX_TOKENS"):
        Settings(_env_file=None, answer_max_tokens=8192, answer_retry_max_tokens=4096)
    with pytest.raises(ValueError):
        Settings(_env_file=None, answer_max_tokens=0)
