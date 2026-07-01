from __future__ import annotations

import json
import types
import urllib.error

import pytest

from brigade.config import Settings
from brigade.providers import (
    AnthropicProvider,
    GeminiProvider,
    LiteLLMProvider,
    OpenAIResponsesProvider,
    ProviderAuthError,
    available_model_options,
    probe_model_inventory,
    provider_from_settings,
)
from brigade.secrets import oauth_credential_status, write_oauth_credential
from brigade.services import build_settings_payload


def test_retired_test_provider_is_not_available(tmp_path):
    settings = Settings(config_path=tmp_path / "brigade.config.json", data_dir=tmp_path)

    payload = available_model_options(settings)

    assert all(option["provider"] != "fake" for option in payload["options"])
    with pytest.raises(ValueError, match="has been removed"):
        provider_from_settings(settings, provider="fake")


def test_unknown_provider_does_not_fall_through_to_litellm(tmp_path):
    settings = Settings(config_path=tmp_path / "brigade.config.json", data_dir=tmp_path)

    with pytest.raises(ValueError, match="unsupported model provider"):
        provider_from_settings(settings, provider="codex")


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


def test_openai_codex_route_uses_codex_auth_mode(tmp_path):
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        default_provider="openai-codex",
        default_model="gpt-5.3-codex-spark",
        openai_auth_mode="api_key",
        openai_codex_auth_mode="oauth",
    )

    provider = provider_from_settings(settings)
    payload = available_model_options(settings)

    assert provider.provider_name == "openai-codex"
    assert provider.model == "gpt-5.3-codex-spark"
    assert provider.auth_mode == "oauth"
    assert provider.api_base == "https://chatgpt.com/backend-api/codex"
    assert payload["recommended"]["provider"] == "openai-codex"
    assert payload["recommended"]["model"] == "gpt-5.3-codex-spark"
    assert payload["recommended"]["available"] is True


def test_openai_responses_provider_uses_native_openai_client(monkeypatch):
    calls = {}

    class FakeResponses:
        def create(self, **kwargs):
            calls["kwargs"] = kwargs
            return {
                "output_text": "done",
                "usage": {"input_tokens": 3, "output_tokens": 2},
            }

    class FakeOpenAI:
        def __init__(self, **kwargs):
            calls["client"] = kwargs
            self.responses = FakeResponses()

    monkeypatch.setitem(
        __import__("sys").modules,
        "openai",
        types.SimpleNamespace(OpenAI=FakeOpenAI),
    )

    provider = OpenAIResponsesProvider(
        model="gpt-5.4",
        api_key="token",
        api_base="https://chatgpt.com/backend-api/codex",
        provider_name="openai-codex",
    )
    response = provider.complete("hello")

    assert calls["client"] == {
        "api_key": "token",
        "base_url": "https://chatgpt.com/backend-api/codex",
    }
    assert calls["kwargs"] == {
        "model": "gpt-5.4",
        "input": [{"role": "user", "content": "hello"}],
        "store": False,
        "stream": True,
    }
    assert response.text == "done"
    assert response.input_tokens == 3
    assert response.output_tokens == 2


def test_openai_responses_provider_collects_streamed_text(monkeypatch):
    class FakeResponses:
        def create(self, **kwargs):
            return iter(
                [
                    types.SimpleNamespace(
                        type="response.output_text.delta",
                        delta="o",
                    ),
                    types.SimpleNamespace(
                        type="response.output_text.delta",
                        delta="k",
                    ),
                    types.SimpleNamespace(
                        type="response.completed",
                        response={
                            "usage": {"input_tokens": 1, "output_tokens": 1},
                        },
                    ),
                ]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.responses = FakeResponses()

    monkeypatch.setitem(
        __import__("sys").modules,
        "openai",
        types.SimpleNamespace(OpenAI=FakeOpenAI),
    )

    provider = OpenAIResponsesProvider(
        model="gpt-5.4",
        api_key="token",
        provider_name="openai-codex",
    )
    response = provider.complete("hello")

    assert response.text == "ok"
    assert response.input_tokens == 1
    assert response.output_tokens == 1


def test_probe_openai_codex_inventory_updates_available_models(tmp_path, monkeypatch):
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        default_provider="openai-codex",
        default_model="gpt-5.3-codex-spark",
        openai_api_key="openai-key",
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "models": [
                        {"slug": "gpt-5.3-codex-spark"},
                        {"slug": "gpt-4.1-mini"},
                    ]
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        assert request.full_url == (
            "https://chatgpt.com/backend-api/codex/models?client_version=1.0.0"
        )
        assert request.headers["Authorization"] == "Bearer openai-key"
        assert timeout == 30
        return FakeResponse()

    monkeypatch.setattr("brigade.providers._list_ollama_models", lambda base_url: [])
    monkeypatch.setattr("brigade.providers.urllib.request.urlopen", fake_urlopen)

    inventory = probe_model_inventory(settings, providers=["openai-codex"])
    payload = available_model_options(settings, inventory)

    assert inventory["providers"]["openai-codex"]["status"] == "ok"
    assert {
        (option["provider"], option["model"])
        for option in payload["options"]
    } >= {
        ("openai-codex", "gpt-5.3-codex-spark"),
        ("openai-codex", "gpt-4.1-mini"),
    }
    assert payload["recommended"]["provider"] == "openai-codex"


def test_openai_codex_model_read_denial_uses_configured_fallback_models(tmp_path, monkeypatch):
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        default_provider="openai-codex",
        default_model="gpt-5.3-codex-spark",
        openai_codex_auth_mode="oauth",
    )
    write_oauth_credential(settings, provider="openai-codex", access_token="token")

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            {},
            None,
        )

    monkeypatch.setattr(
        "brigade.providers._read_http_error",
        lambda exc: '{"error":"Missing scopes: api.model.read"}',
    )

    monkeypatch.setattr("brigade.providers.urllib.request.urlopen", fake_urlopen)

    inventory = probe_model_inventory(settings, providers=["openai-codex"])
    payload = available_model_options(settings, inventory)

    assert inventory["providers"]["openai-codex"]["status"] == "limited"
    models = {
        option["model"]
        for option in payload["options"]
        if option["provider"] == "openai-codex"
    }
    assert {"gpt-5.3-codex-spark", "gpt-5.4"} <= models


def test_anthropic_provider_missing_api_key_raises_auth_error():
    provider = AnthropicProvider(model="claude-sonnet-4-6", api_key=None)

    with pytest.raises(ProviderAuthError, match="ANTHROPIC_API_KEY"):
        provider.complete("hello")


def test_anthropic_provider_calls_native_sdk(monkeypatch):
    calls = {}

    class FakeContent:
        text = "world"

    class FakeUsage:
        input_tokens = 4
        output_tokens = 7

    class FakeMessages:
        def create(self, **kwargs):
            calls["kwargs"] = kwargs
            result = types.SimpleNamespace(
                content=[FakeContent()],
                usage=FakeUsage(),
            )
            return result

    class FakeAnthropic:
        def __init__(self, **kwargs):
            calls["client"] = kwargs
            self.messages = FakeMessages()

    monkeypatch.setitem(
        __import__("sys").modules,
        "anthropic",
        types.SimpleNamespace(Anthropic=FakeAnthropic),
    )

    provider = AnthropicProvider(model="claude-sonnet-4-6", api_key="sk-ant-key")
    response = provider.complete("hello")

    assert calls["client"] == {"api_key": "sk-ant-key"}
    assert calls["kwargs"]["model"] == "claude-sonnet-4-6"
    assert calls["kwargs"]["messages"] == [{"role": "user", "content": "hello"}]
    assert "max_tokens" in calls["kwargs"]
    assert response.text == "world"
    assert response.input_tokens == 4
    assert response.output_tokens == 7
    assert response.provider == "anthropic"


def test_anthropic_provider_maps_auth_error(monkeypatch):
    class FakeMessages:
        def create(self, **kwargs):
            raise Exception("401 Unauthorized invalid api-key")

    class FakeAnthropic:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setitem(
        __import__("sys").modules,
        "anthropic",
        types.SimpleNamespace(Anthropic=FakeAnthropic),
    )

    provider = AnthropicProvider(model="claude-sonnet-4-6", api_key="bad-key")

    with pytest.raises(ProviderAuthError, match="credential failed"):
        provider.complete("hello")


def test_provider_from_settings_routes_anthropic_to_native_provider(tmp_path):
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        anthropic_api_key="sk-ant-key",
        default_provider="anthropic",
        default_model="claude-sonnet-4-6",
    )

    provider = provider_from_settings(settings)

    assert isinstance(provider, AnthropicProvider)
    assert provider.model == "claude-sonnet-4-6"
    assert provider.api_key == "sk-ant-key"


def test_gemini_provider_missing_api_key_raises_auth_error():
    provider = GeminiProvider(model="gemini-1.5-flash", api_key=None)

    with pytest.raises(ProviderAuthError, match="GEMINI_API_KEY"):
        provider.complete("hello")


def test_gemini_provider_calls_openai_compat_endpoint(monkeypatch):
    calls = {}

    class FakeMessage:
        content = "gemini says hello"

    class FakeChoice:
        message = FakeMessage()

    class FakeUsage:
        prompt_tokens = 5
        completion_tokens = 8

    class FakeCompletions:
        def create(self, **kwargs):
            calls["kwargs"] = kwargs
            return types.SimpleNamespace(choices=[FakeChoice()], usage=FakeUsage())

    class FakeChat:
        completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            calls["client"] = kwargs
            self.chat = FakeChat()

    monkeypatch.setitem(
        __import__("sys").modules,
        "openai",
        types.SimpleNamespace(OpenAI=FakeOpenAI),
    )

    provider = GeminiProvider(model="gemini-1.5-flash", api_key="gemini-key")
    response = provider.complete("hello")

    assert calls["client"]["base_url"].startswith("https://generativelanguage.googleapis.com")
    assert calls["kwargs"]["model"] == "gemini-1.5-flash"
    assert calls["kwargs"]["messages"] == [{"role": "user", "content": "hello"}]
    assert response.text == "gemini says hello"
    assert response.input_tokens == 5
    assert response.output_tokens == 8
    assert response.provider == "gemini"


def test_gemini_provider_strips_litellm_prefix(monkeypatch):
    class FakeMessage:
        content = "ok"

    class FakeChoice:
        message = FakeMessage()

    class FakeUsage:
        prompt_tokens = 1
        completion_tokens = 1

    class FakeCompletions:
        def create(self, **kwargs):
            return types.SimpleNamespace(choices=[FakeChoice()], usage=FakeUsage())

    class FakeChat:
        completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = FakeChat()

    monkeypatch.setitem(
        __import__("sys").modules,
        "openai",
        types.SimpleNamespace(OpenAI=FakeOpenAI),
    )

    provider = GeminiProvider(model="gemini/gemini-1.5-flash", api_key="gemini-key")

    assert provider.model == "gemini-1.5-flash"


def test_provider_from_settings_routes_gemini_to_native_provider(tmp_path):
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        gemini_api_key="gemini-key",
        default_provider="gemini",
        default_model="gemini-1.5-flash",
    )

    provider = provider_from_settings(settings)

    assert isinstance(provider, GeminiProvider)
    assert provider.model == "gemini-1.5-flash"
    assert provider.api_key == "gemini-key"


def test_probe_anthropic_inventory_lists_models(tmp_path, monkeypatch):
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        anthropic_api_key="sk-ant-key",
        default_provider="anthropic",
        default_model="claude-sonnet-4-6",
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps({
                "data": [
                    {"id": "claude-sonnet-4-6", "type": "model"},
                    {"id": "claude-haiku-4-5", "type": "model"},
                ]
            }).encode()

    def fake_urlopen(request, timeout):
        assert "api.anthropic.com/v1/models" in request.full_url
        assert request.headers.get("X-api-key") == "sk-ant-key"
        assert "Anthropic-version" in request.headers
        return FakeResponse()

    monkeypatch.setattr("brigade.providers.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("brigade.providers._list_ollama_models", lambda _: [])

    inventory = probe_model_inventory(settings, providers=["anthropic"])
    payload = available_model_options(settings, inventory)

    assert inventory["providers"]["anthropic"]["status"] == "ok"
    models = {o["model"] for o in payload["options"] if o["provider"] == "anthropic"}
    assert {"claude-sonnet-4-6", "claude-haiku-4-5"} <= models


def test_probe_gemini_inventory_lists_models(tmp_path, monkeypatch):
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        gemini_api_key="gemini-key",
        default_provider="gemini",
        default_model="gemini-1.5-flash",
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps({
                "data": [
                    {"id": "gemini-1.5-flash"},
                    {"id": "gemini-1.5-pro"},
                    {"id": "text-embedding-004"},  # should be excluded
                ]
            }).encode()

    def fake_urlopen(request, timeout):
        assert "generativelanguage.googleapis.com" in request.full_url
        assert request.headers.get("Authorization") == "Bearer gemini-key"
        return FakeResponse()

    monkeypatch.setattr("brigade.providers.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("brigade.providers._list_ollama_models", lambda _: [])

    inventory = probe_model_inventory(settings, providers=["gemini"])
    payload = available_model_options(settings, inventory)

    assert inventory["providers"]["gemini"]["status"] == "ok"
    models = {o["model"] for o in payload["options"] if o["provider"] == "gemini"}
    assert "gemini-1.5-flash" in models
    assert "gemini-1.5-pro" in models
    assert "text-embedding-004" not in models


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
