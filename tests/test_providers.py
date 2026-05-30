from __future__ import annotations

import json
import types

import pytest

from brigade.config import Settings
from brigade.providers import (
    FakeProvider,
    LiteLLMProvider,
    ProviderAuthError,
    available_model_options,
    provider_from_settings,
)
from brigade.secrets import oauth_credential_status, write_oauth_credential
from brigade.services import build_settings_payload


def test_fake_provider_is_deterministic():
    response = FakeProvider().complete("Summarize the current mission")

    assert response.provider == "fake"
    assert response.model == "deterministic"
    assert response.text.startswith("FAKE_RESPONSE:")
    assert response.input_tokens == 4


def test_litellm_provider_missing_api_key_is_actionable_without_importing_litellm():
    provider = LiteLLMProvider(model="gpt-4.1-mini", provider_name="openai")

    with pytest.raises(ProviderAuthError, match="OPENAI_API_KEY"):
        provider.complete("hello")


def test_litellm_provider_maps_invalid_credentials(monkeypatch):
    def failing_completion(**kwargs):
        raise Exception("401 invalid api key sk-test-secret")

    monkeypatch.setitem(
        __import__("sys").modules,
        "litellm",
        types.SimpleNamespace(completion=failing_completion),
    )
    provider = LiteLLMProvider(
        model="gpt-4.1-mini",
        api_key="key",
        provider_name="openai",
    )

    with pytest.raises(ProviderAuthError, match="credential failed"):
        provider.complete("hello")


def test_litellm_provider_records_cost_when_litellm_reports_it(monkeypatch):
    response_payload = {
        "choices": [{"message": {"content": "done"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }

    def completion(**kwargs):
        return response_payload

    def completion_cost(completion_response):
        assert completion_response == response_payload
        return 0.00123

    monkeypatch.setitem(
        __import__("sys").modules,
        "litellm",
        types.SimpleNamespace(completion=completion, completion_cost=completion_cost),
    )
    provider = LiteLLMProvider(model="test-model", provider_name="litellm")

    response = provider.complete("hello")

    assert response.input_tokens == 10
    assert response.output_tokens == 5
    assert response.estimated_cost_usd == 0.00123


def test_oauth_secret_store_status_is_redacted_and_provider_uses_credential(tmp_path):
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        secret_store_path=tmp_path / "secrets",
        openai_auth_mode="oauth",
    )

    status = write_oauth_credential(
        settings,
        provider="openai",
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_in=3600,
        client_id="client-id",
        client_secret="client-secret",
    )
    provider = provider_from_settings(settings, provider="openai", model="gpt-4.1-mini")
    payload = build_settings_payload(settings)

    assert status["access_token"] == "***redacted***"
    assert status["refresh_token"] == "***redacted***"
    assert oauth_credential_status(settings, "openai")["client_secret"] == "***redacted***"
    assert provider.auth_mode == "oauth"
    assert provider.oauth_credential["access_token"] == "access-secret"
    assert payload["openai_auth_mode"] == "oauth"
    assert "access-secret" not in json.dumps(payload)


def test_available_models_recommend_fast_ollama_model_when_default_missing(tmp_path, monkeypatch):
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        default_provider="ollama",
        default_model="llama3.1",
    )
    monkeypatch.setattr(
        "brigade.providers._list_ollama_models",
        lambda base_url: [
            "devstral-small-2:latest",
            "gemma4:26b",
            "nomic-embed-text:latest",
            "qwen2.5-coder:7b",
        ],
    )

    payload = available_model_options(settings)

    assert payload["default"]["available"] is False
    assert payload["recommended"]["model"] == "qwen2.5-coder:7b"
