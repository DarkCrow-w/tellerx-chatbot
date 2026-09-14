"""Connectivity diagnostics must exercise the configured production request."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.commands import diagnostics
from app.core.config import Settings
from app.integrations.openai_client import ChatCallResult, Usage


def test_diagnostics_uses_registry_model_and_configured_budget(monkeypatch, tmp_path):
    registry = tmp_path / "models.yaml"
    registry.write_text(
        "models:\n  - {id: configured-model, tier: all, quota_tokens: 10000, priority: 1, enabled: true, stable: true}\n"
    )
    settings = Settings(_env_file=None, model_registry_path=registry, answer_max_tokens=8192)
    client = Mock()
    client.chat_json.return_value = ChatCallResult(
        "configured-model", "request", '{"status":"ok"}', Usage(), 1
    )
    monkeypatch.setattr(diagnostics, "get_settings", lambda: settings)
    monkeypatch.setattr(diagnostics, "model_client", lambda: client)
    monkeypatch.setattr("sys.argv", ["model-diagnostics", "--skip-embedding"])
    with pytest.raises(SystemExit) as caught:
        diagnostics.diagnostics_main()
    assert caught.value.code == 0
    assert client.chat_json.call_args.kwargs["model_id"] == "configured-model"
    assert client.chat_json.call_args.kwargs["max_tokens"] == 8192


def test_diagnostics_rejects_invalid_json(monkeypatch, capsys):
    client = Mock()
    client.chat_json.return_value = SimpleNamespace(
        content="not JSON", model_id="explicit", usage=Usage()
    )
    monkeypatch.setattr(diagnostics, "model_client", lambda: client)
    monkeypatch.setattr(
        "sys.argv", ["model-diagnostics", "--chat-model", "explicit", "--skip-embedding"]
    )
    with pytest.raises(SystemExit) as caught:
        diagnostics.diagnostics_main()
    assert caught.value.code == 1
    assert "invalid_json" in capsys.readouterr().out
