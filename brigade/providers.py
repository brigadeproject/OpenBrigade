from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from brigade.secrets import oauth_credential_expired, read_oauth_credential
from brigade.time import utc_now_iso

PREFERRED_OLLAMA_MODELS = (
    "qwen2.5-coder:7b",
    "qwen2.5:7b",
    "llama3.1:8b",
    "llama3:8b",
    "mistral:7b",
    "devstral-small",
)
RETIRED_MODEL_PROVIDERS = {"fake"}
SUPPORTED_MODEL_PROVIDERS = {
    "ollama",
    "litellm",
    "openai",
    "openai-codex",
    "anthropic",
    "gemini",
}
SUPPORTED_AUTH_MODES = {"api_key", "oauth"}
OPENAI_RESPONSES_BASE_URL = "https://api.openai.com/v1"
OPENAI_CODEX_RESPONSES_BASE_URL = "https://chatgpt.com/backend-api/codex"
GEMINI_OPENAI_COMPAT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
ANTHROPIC_DEFAULT_MAX_TOKENS = 8192
OPENAI_CODEX_FALLBACK_MODELS = (
    "gpt-5.3-codex-spark",
    "gpt-5.4",
)


@dataclass(frozen=True)
class ModelResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    provider: str = "unknown"
    model: str = "unknown"
    route_type: str = "unknown"
    estimated_cost_usd: float = 0.0


class ModelProvider(Protocol):
    def complete(self, prompt: str) -> ModelResponse:
        """Return one completion for a prompt."""


class ProviderAuthError(RuntimeError):
    """Actionable model-provider credential failure."""


class ModelUnavailableError(RuntimeError):
    """Configured provider model is not installed or otherwise unavailable."""


@dataclass(frozen=True)
class ModelOption:
    provider: str
    model: str
    label: str
    route_type: str
    available: bool
    configured: bool = True
    base_url: str | None = None
    detail: str | None = None
    is_default: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "label": self.label,
            "route_type": self.route_type,
            "available": self.available,
            "configured": self.configured,
            "base_url": self.base_url,
            "detail": self.detail,
            "is_default": self.is_default,
        }


class OllamaProvider:
    route_type = "local"
    supports_native_tools = True

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "gpt-oss:20b",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

    def complete(
        self, prompt: str, tools: list[dict[str, Any]] | None = None
    ) -> ModelResponse:
        # Use /api/chat (not the legacy /api/generate): chat returns the
        # assistant's answer in message.content separately from any reasoning,
        # so "thinking" models populate content instead of leaving it empty.
        #
        # Declaring tools natively matters for models with a built-in tool-call
        # syntax (e.g. gpt-oss): if the model emits that syntax while no tools
        # are declared, Ollama's parser fails the whole request with HTTP 500
        # "error parsing tool call". With tools declared, Ollama returns
        # structured message.tool_calls instead.
        # Ollama defaults to a 4096-token context, which brigade's assignment
        # prompts routinely exceed; overflow either errors (llama-server) or
        # silently degrades into empty assistant messages. Request a real
        # window explicitly.
        num_ctx = int(os.environ.get("BRIGADE_OLLAMA_NUM_CTX", "16384"))
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"num_ctx": num_ctx},
        }
        if tools:
            body["tools"] = tools
        payload = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                data = json.loads(response.read().decode("utf-8"))
        except TimeoutError as exc:
            raise RuntimeError(
                f"ollama request timed out for model '{self.model}' at {self.base_url}; "
                "choose a smaller loaded model or retry after the model is warm"
            ) from exc
        except urllib.error.HTTPError as exc:
            body = _read_http_error(exc)
            if exc.code == 404:
                raise ModelUnavailableError(
                    f"ollama model '{self.model}' is not available at {self.base_url}; "
                    "choose an installed model or pull it with Ollama"
                ) from exc
            raise RuntimeError(
                f"ollama request failed: HTTP {exc.code}: {_sanitize_error_message(body)}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"ollama request failed: {exc}") from exc

        message = data.get("message")
        text = str((message or {}).get("content", "") or "")
        tool_calls = (message or {}).get("tool_calls") or []
        if tool_calls:
            # Translate the native tool call into the brigade agent response
            # protocol so the runner's parser handles it uniformly.
            function = tool_calls[0].get("function") or {}
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {"raw": arguments}
            tool_name = str(function.get("name") or "")
            text = json.dumps(
                {
                    "status": "tool_call",
                    "tool": tool_name,
                    "arguments": arguments,
                    "summary": text.strip()[:300] or f"native tool call: {tool_name}",
                }
            )
        elif not text.strip():
            # A reasoning-only model can return content="" (everything went to
            # message.thinking). Fail loudly so this surfaces as a provider/config
            # error (alert + deferral) instead of masquerading as a task blocker.
            raise ModelUnavailableError(
                f"ollama model '{self.model}' returned an empty assistant message; "
                "it may be a reasoning-only/thinking model that emits no content over "
                "/api/chat — choose a chat/instruct model (e.g. qwen2.5-coder:7b)"
            )
        return ModelResponse(
            text=text,
            input_tokens=int(data.get("prompt_eval_count", 0) or 0),
            output_tokens=int(data.get("eval_count", 0) or 0),
            provider="ollama",
            model=self.model,
            route_type=self.route_type,
        )


class LiteLLMProvider:
    route_type = "cloud"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        api_base: str | None = None,
        provider_name: str = "litellm",
        auth_mode: str = "api_key",
        oauth_credential: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.provider_name = provider_name
        self.auth_mode = auth_mode
        self.oauth_credential = oauth_credential

    def complete(self, prompt: str) -> ModelResponse:
        api_key = self._resolved_api_key()
        try:
            import litellm
        except ImportError as exc:
            raise RuntimeError("litellm is not installed; install the models extra") from exc

        try:
            response = litellm.completion(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                api_key=api_key,
                api_base=self.api_base,
            )
        except Exception as exc:
            raise _map_litellm_error(self.provider_name, exc) from exc
        choice = response["choices"][0]["message"]["content"]
        usage = response.get("usage", {})
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        return ModelResponse(
            text=str(choice or ""),
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            provider=self.provider_name,
            model=self.model,
            route_type=self.route_type,
            estimated_cost_usd=_litellm_cost_usd(litellm, response),
        )

    def _resolved_api_key(self) -> str | None:
        if self.auth_mode == "api_key":
            known_key_providers = {"openai", "openai-codex", "anthropic", "gemini"}
            if self.provider_name in known_key_providers and not self.api_key:
                env_name = (
                    "GEMINI_API_KEY"
                    if self.provider_name == "gemini"
                    else "ANTHROPIC_API_KEY"
                    if self.provider_name == "anthropic"
                    else "OPENAI_API_KEY"
                )
                raise ProviderAuthError(
                    f"{self.provider_name} API key is missing; set {env_name} "
                    "or run 'brigade model auth login --method oauth'."
                )
            return self.api_key
        if self.auth_mode == "oauth":
            credential = self.oauth_credential
            if not credential:
                raise ProviderAuthError(
                    f"{self.provider_name} OAuth credentials are missing; run "
                    f"'brigade model auth login --provider {self.provider_name} --method oauth'."
                )
            if oauth_credential_expired(credential):
                raise ProviderAuthError(
                    f"{self.provider_name} OAuth access token is expired; rerun model auth login."
                )
            token = credential.get("access_token") or credential.get("refresh_token")
            if not token:
                raise ProviderAuthError(
                    f"{self.provider_name} OAuth credential has no usable token; rerun "
                    "model auth login."
                )
            return str(token)
        raise ProviderAuthError(
            f"unsupported auth mode for {self.provider_name}: {self.auth_mode}"
        )


def _resolve_bearer_token(
    provider_name: str,
    api_key: str | None,
    auth_mode: str,
    oauth_credential: dict[str, Any] | None,
    *,
    key_env_var: str = "OPENAI_API_KEY",
) -> str:
    if auth_mode == "api_key":
        if not api_key:
            raise ProviderAuthError(
                f"{provider_name} API key is missing; set {key_env_var} "
                "or run 'brigade model auth login --method oauth'."
            )
        return api_key
    if auth_mode == "oauth":
        if not oauth_credential:
            raise ProviderAuthError(
                f"{provider_name} OAuth credentials are missing; run "
                f"'brigade model auth login --provider {provider_name} --method oauth'."
            )
        if oauth_credential_expired(oauth_credential):
            raise ProviderAuthError(
                f"{provider_name} OAuth access token is expired; rerun model auth login."
            )
        token = oauth_credential.get("access_token") or oauth_credential.get("refresh_token")
        if not token:
            raise ProviderAuthError(
                f"{provider_name} OAuth credential has no usable token; rerun model auth login."
            )
        return str(token)
    raise ProviderAuthError(f"unsupported auth mode for {provider_name}: {auth_mode}")


class AnthropicProvider:
    route_type = "cloud"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        provider_name: str = "anthropic",
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.provider_name = provider_name

    def complete(self, prompt: str) -> ModelResponse:
        if not self.api_key:
            raise ProviderAuthError(
                f"{self.provider_name} API key is missing; set ANTHROPIC_API_KEY."
            )
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise RuntimeError("anthropic is not installed; install the models extra") from exc

        try:
            client = Anthropic(api_key=self.api_key)
            response = client.messages.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=ANTHROPIC_DEFAULT_MAX_TOKENS,
            )
        except Exception as exc:
            raise _map_provider_error(self.provider_name, exc) from exc

        text = "".join(
            block.text for block in (response.content or []) if hasattr(block, "text")
        )
        usage = response.usage
        return ModelResponse(
            text=text,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            provider=self.provider_name,
            model=self.model,
            route_type=self.route_type,
        )


class GeminiProvider:
    route_type = "cloud"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        provider_name: str = "gemini",
        auth_mode: str = "api_key",
        oauth_credential: dict[str, Any] | None = None,
    ) -> None:
        self.model = model.removeprefix("gemini/")
        self.api_key = api_key
        self.provider_name = provider_name
        self.auth_mode = auth_mode
        self.oauth_credential = oauth_credential

    def complete(self, prompt: str) -> ModelResponse:
        api_key = _resolve_bearer_token(
            self.provider_name,
            self.api_key,
            self.auth_mode,
            self.oauth_credential,
            key_env_var="GEMINI_API_KEY",
        )
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("openai is not installed; install the models extra") from exc

        try:
            client = OpenAI(api_key=api_key, base_url=GEMINI_OPENAI_COMPAT_BASE_URL)
            response = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            raise _map_provider_error(self.provider_name, exc) from exc

        text = str((response.choices[0].message.content or "") if response.choices else "")
        usage = response.usage
        return ModelResponse(
            text=text,
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            provider=self.provider_name,
            model=self.model,
            route_type=self.route_type,
        )


class OpenAIResponsesProvider:
    route_type = "cloud"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        api_base: str | None = None,
        provider_name: str = "openai",
        auth_mode: str = "api_key",
        oauth_credential: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.api_base = api_base or (
            OPENAI_CODEX_RESPONSES_BASE_URL
            if provider_name == "openai-codex"
            else OPENAI_RESPONSES_BASE_URL
        )
        self.provider_name = provider_name
        self.auth_mode = auth_mode
        self.oauth_credential = oauth_credential

    def complete(self, prompt: str) -> ModelResponse:
        api_key = _resolve_bearer_token(
            self.provider_name,
            self.api_key,
            self.auth_mode,
            self.oauth_credential,
            key_env_var="OPENAI_API_KEY",
        )
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("openai is not installed; install the models extra") from exc

        try:
            client = OpenAI(api_key=api_key, base_url=self.api_base)
            response = client.responses.create(
                model=self.model,
                input=[{"role": "user", "content": prompt}],
                store=False,
                stream=True,
            )
        except Exception as exc:
            raise _map_provider_error(self.provider_name, exc) from exc

        streamed_text, final_response = _collect_response_stream(response)
        text = streamed_text if streamed_text else _response_output_text(final_response)

        return ModelResponse(
            text=text,
            input_tokens=_response_usage_tokens(final_response, "input_tokens"),
            output_tokens=_response_usage_tokens(final_response, "output_tokens"),
            provider=self.provider_name,
            model=self.model,
            route_type=self.route_type,
        )

    def _resolved_api_key(self) -> str:
        return _resolve_bearer_token(
            self.provider_name,
            self.api_key,
            self.auth_mode,
            self.oauth_credential,
            key_env_var="OPENAI_API_KEY",
        )


def provider_from_settings(
    settings: Any,
    *,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    api_base: str | None = None,
):
    provider_name = provider or settings.default_provider
    model_name = model or settings.default_model
    if provider_name in RETIRED_MODEL_PROVIDERS:
        raise ValueError(
            f"model provider '{provider_name}' has been removed; "
            "configure ollama or a cloud provider"
        )
    if provider_name not in SUPPORTED_MODEL_PROVIDERS:
        raise ValueError(
            f"unsupported model provider '{provider_name}'; choose one of "
            f"{', '.join(sorted(SUPPORTED_MODEL_PROVIDERS))}"
        )
    if provider_name == "ollama":
        return OllamaProvider(base_url=api_base or settings.ollama_base_url, model=model_name)
    if provider_name in {"openai", "openai-codex"}:
        auth_mode = (
            settings.openai_codex_auth_mode
            if provider_name == "openai-codex"
            else settings.openai_auth_mode
        )
        return OpenAIResponsesProvider(
            model=model_name,
            api_key=api_key or settings.openai_api_key,
            api_base=api_base
            or (
                OPENAI_CODEX_RESPONSES_BASE_URL
                if provider_name == "openai-codex"
                else OPENAI_RESPONSES_BASE_URL
            ),
            provider_name=provider_name,
            auth_mode=auth_mode,
            oauth_credential=read_oauth_credential(settings, provider_name)
            if auth_mode == "oauth"
            else None,
        )
    if provider_name == "anthropic":
        return AnthropicProvider(
            model=model_name,
            api_key=api_key or settings.anthropic_api_key,
            provider_name="anthropic",
        )
    if provider_name == "gemini":
        return GeminiProvider(
            model=model_name,
            api_key=api_key or settings.gemini_api_key,
            provider_name="gemini",
            auth_mode=settings.gemini_auth_mode,
            oauth_credential=read_oauth_credential(settings, "gemini")
            if settings.gemini_auth_mode == "oauth"
            else None,
        )
    return LiteLLMProvider(
        model=model_name,
        api_key=api_key,
        api_base=api_base,
        provider_name=provider_name,
    )


def available_model_options(
    settings: Any,
    inventory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    default_provider = settings.default_provider
    default_model = settings.default_model
    options: list[ModelOption] = []
    options.extend(_ollama_model_options(settings))
    options.extend(_inventory_model_options(settings, inventory))

    openai_codex_auth_mode = getattr(
        settings,
        "openai_codex_auth_mode",
        getattr(settings, "openai_auth_mode", "api_key"),
    )
    if (
        settings.openai_api_key
        or settings.openai_auth_mode == "oauth"
        or openai_codex_auth_mode == "oauth"
        or default_provider in {"openai", "openai-codex"}
    ):
        for provider_name in ("openai", "openai-codex"):
            auth_mode = (
                openai_codex_auth_mode
                if provider_name == "openai-codex"
                else getattr(settings, "openai_auth_mode", "api_key")
            )
            configured = bool(settings.openai_api_key) or auth_mode == "oauth"
            fallback_models = (
                _openai_codex_fallback_models(default_model)
                if provider_name == "openai-codex"
                else (default_model if default_provider == provider_name else "gpt-4.1-mini",)
            )
            for model in fallback_models:
                options.append(
                    ModelOption(
                        provider=provider_name,
                        model=model,
                        label=f"{provider_name} / {model}",
                        route_type="cloud",
                        available=configured,
                        configured=configured,
                        detail=None if configured else "OpenAI credentials are not configured",
                        is_default=default_provider == provider_name and default_model == model,
                    )
                )

    if (
        settings.gemini_api_key
        or settings.gemini_auth_mode == "oauth"
        or default_provider == "gemini"
    ):
        configured = bool(settings.gemini_api_key) or settings.gemini_auth_mode == "oauth"
        model = default_model if default_provider == "gemini" else "gemini-1.5-flash"
        options.append(
            ModelOption(
                provider="gemini",
                model=model,
                label=f"Gemini / {model}",
                route_type="cloud",
                available=configured,
                configured=configured,
                detail=None if configured else "Gemini credentials are not configured",
                is_default=default_provider == "gemini" and default_model == model,
            )
        )

    if settings.anthropic_api_key or default_provider == "anthropic":
        configured = bool(settings.anthropic_api_key)
        model = default_model if default_provider == "anthropic" else "claude-sonnet-4-6"
        options.append(
            ModelOption(
                provider="anthropic",
                model=model,
                label=f"Anthropic / {model}",
                route_type="cloud",
                available=configured,
                configured=configured,
                detail=None if configured else "Anthropic credentials are not configured",
                is_default=default_provider == "anthropic" and default_model == model,
            )
        )

    if default_provider == "litellm":
        options.append(
            ModelOption(
                provider="litellm",
                model=default_model,
                label=f"LiteLLM / {default_model}",
                route_type="cloud",
                available=True,
                configured=True,
                is_default=True,
            )
        )

    options = _dedupe_model_options(options)
    default = next((item for item in options if item.is_default), None)
    if default is None:
        default = ModelOption(
            provider=default_provider,
            model=default_model,
            label=f"{default_provider} / {default_model}",
            route_type="unknown",
            available=False,
            configured=False,
            detail="Configured default route is not available",
            is_default=True,
        )
        options.append(default)
    recommended = _recommended_option(options, default_provider, default_model)

    return {
        "default": default.to_dict(),
        "recommended": recommended.to_dict(),
        "options": [item.to_dict() for item in options],
        "inventory": inventory or {"providers": {}, "updated_at": None},
    }


def probe_model_inventory(
    settings: Any,
    *,
    providers: list[str] | None = None,
) -> dict[str, Any]:
    """Probe configured model providers and return a cacheable inventory payload."""
    selected = providers or _default_probe_providers(settings)
    records: dict[str, Any] = {}
    for provider_name in selected:
        records[provider_name] = _probe_provider_models(settings, provider_name)
    return {"providers": records, "updated_at": utc_now_iso()}


def _default_probe_providers(settings: Any) -> list[str]:
    providers = ["ollama", settings.default_provider]
    if (
        settings.openai_api_key
        or settings.openai_auth_mode == "oauth"
        or getattr(settings, "openai_codex_auth_mode", "api_key") == "oauth"
    ):
        providers.extend(["openai", "openai-codex"])
    if settings.gemini_api_key or settings.gemini_auth_mode == "oauth":
        providers.append("gemini")
    if settings.anthropic_api_key:
        providers.append("anthropic")
    return list(dict.fromkeys(item for item in providers if item in SUPPORTED_MODEL_PROVIDERS))


def _probe_provider_models(settings: Any, provider_name: str) -> dict[str, Any]:
    probed_at = utc_now_iso()
    base = {
        "provider": provider_name,
        "probed_at": probed_at,
        "route_type": "local" if provider_name == "ollama" else "cloud",
        "models": [],
    }
    try:
        if provider_name == "ollama":
            names = _list_ollama_models(settings.ollama_base_url.rstrip("/"))
            return {
                **base,
                "status": "ok",
                "models": [
                    _inventory_model(
                        provider_name,
                        name,
                        "local",
                        base_url=settings.ollama_base_url,
                    )
                    for name in names
                ],
            }
        if provider_name in {"openai", "openai-codex"}:
            return {
                **base,
                "status": "ok",
                "models": [
                    _inventory_model(provider_name, name, "cloud")
                    for name in _list_openai_models(settings, provider_name)
                ],
            }
        if provider_name == "anthropic":
            return {
                **base,
                "status": "ok",
                "models": [
                    _inventory_model(provider_name, name, "cloud")
                    for name in _list_anthropic_models(settings)
                ],
            }
        if provider_name == "gemini":
            return {
                **base,
                "status": "ok",
                "models": [
                    _inventory_model(provider_name, name, "cloud")
                    for name in _list_gemini_models(settings)
                ],
            }
        return {
            **base,
            "status": "skipped",
            "detail": f"model probing is not implemented for {provider_name}",
        }
    except Exception as exc:
        mapped = _map_litellm_error(provider_name, exc)
        if provider_name == "openai-codex" and "api.model.read" in str(mapped):
            return {
                **base,
                "status": "limited",
                "detail": (
                    "model enumeration requires api.model.read; using configured Codex "
                    "fallback models"
                ),
                "models": [
                    _inventory_model(provider_name, name, "cloud", detail="configured fallback")
                    for name in _openai_codex_fallback_models(settings.default_model)
                ],
            }
        return {
            **base,
            "status": "error",
            "detail": _sanitize_error_message(str(mapped)),
        }


def _inventory_model(
    provider: str,
    model: str,
    route_type: str,
    *,
    base_url: str | None = None,
    detail: str = "provider probe",
) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": model,
        "label": f"{provider} / {model}",
        "route_type": route_type,
        "available": True,
        "configured": True,
        "base_url": base_url,
        "detail": detail,
    }


def is_model_not_found_error(exc: Exception | str) -> bool:
    """A 404 for the model itself (e.g. listed by the provider but not callable
    for this account), as opposed to transient or auth failures."""
    message = str(exc).lower()
    if "model_not_found" in message or "model not found" in message:
        return True
    return "404" in message and "'param': 'model'" in message


def demote_unavailable_model(
    store: Any,
    provider_name: str,
    model: str,
    *,
    detail: str = "listed by provider but not callable (404 on last run)",
) -> bool:
    """Mark a probed model unavailable after a runtime 404 so agents stop
    routing to it. A later successful probe/refresh restores it."""
    inventory = store.model_inventory() or {}
    providers = inventory.get("providers")
    if not isinstance(providers, dict):
        return False
    record = providers.get(provider_name)
    if not isinstance(record, dict):
        return False
    changed = False
    for item in record.get("models", []):
        if (
            isinstance(item, dict)
            and item.get("model") == model
            and item.get("available", True)
        ):
            item["available"] = False
            item["detail"] = detail
            changed = True
    if changed:
        store.set_model_inventory(inventory)
    return changed


def _openai_codex_fallback_models(default_model: str) -> tuple[str, ...]:
    models = [default_model, *OPENAI_CODEX_FALLBACK_MODELS]
    return tuple(dict.fromkeys(model for model in models if model))


def _list_openai_models(settings: Any, provider_name: str) -> list[str]:
    provider = provider_from_settings(settings, provider=provider_name)
    if not isinstance(provider, OpenAIResponsesProvider):
        return []
    api_key = provider._resolved_api_key()
    api_base = (provider.api_base or OPENAI_RESPONSES_BASE_URL).rstrip("/")
    url = urllib.parse.urljoin(f"{api_base}/", "models")
    if provider_name == "openai-codex":
        url = f"{url}?{urllib.parse.urlencode({'client_version': '1.0.0'})}"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = _read_http_error(exc)
        raise RuntimeError(
            f"model list failed: HTTP {exc.code}: {_sanitize_error_message(body)}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"model list failed: {exc}") from exc
    data = None
    if isinstance(payload, dict):
        data = payload.get("data")
        if data is None and provider_name == "openai-codex":
            data = payload.get("models")
    if not isinstance(data, list):
        return []
    names = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or item.get("slug") or "").strip()
        if model_id:
            names.append(model_id)
    return sorted(set(names))


def _list_anthropic_models(settings: Any) -> list[str]:
    provider = provider_from_settings(settings, provider="anthropic")
    if not isinstance(provider, AnthropicProvider) or not provider.api_key:
        return []
    url = "https://api.anthropic.com/v1/models"
    request = urllib.request.Request(
        url,
        headers={
            "x-api-key": provider.api_key,
            "anthropic-version": "2023-06-01",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = _read_http_error(exc)
        raise RuntimeError(
            f"model list failed: HTTP {exc.code}: {_sanitize_error_message(body)}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"model list failed: {exc}") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    return sorted(
        str(item["id"]).strip()
        for item in data
        if isinstance(item, dict) and item.get("id")
    )


def _list_gemini_models(settings: Any) -> list[str]:
    provider = provider_from_settings(settings, provider="gemini")
    if not isinstance(provider, GeminiProvider):
        return []
    try:
        api_key = _resolve_bearer_token(
            provider.provider_name,
            provider.api_key,
            provider.auth_mode,
            provider.oauth_credential,
            key_env_var="GEMINI_API_KEY",
        )
    except ProviderAuthError:
        return []
    url = "https://generativelanguage.googleapis.com/v1beta/openai/models"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = _read_http_error(exc)
        raise RuntimeError(
            f"model list failed: HTTP {exc.code}: {_sanitize_error_message(body)}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"model list failed: {exc}") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    # Exclude embedding and tuned models; keep generative Gemini models only.
    return sorted(
        str(item["id"]).strip()
        for item in data
        if isinstance(item, dict)
        and item.get("id")
        and str(item["id"]).startswith("gemini-")
    )


def _inventory_model_options(
    settings: Any,
    inventory: dict[str, Any] | None,
) -> list[ModelOption]:
    if not inventory:
        return []
    providers = inventory.get("providers")
    if not isinstance(providers, dict):
        return []
    options: list[ModelOption] = []
    for record in providers.values():
        if not isinstance(record, dict) or record.get("status") not in {"ok", "limited"}:
            continue
        models = record.get("models")
        if not isinstance(models, list):
            continue
        for item in models:
            if not isinstance(item, dict):
                continue
            provider = str(item.get("provider") or record.get("provider") or "").strip()
            model = str(item.get("model") or "").strip()
            if not provider or not model:
                continue
            options.append(
                ModelOption(
                    provider=provider,
                    model=model,
                    label=str(item.get("label") or f"{provider} / {model}"),
                    route_type=str(item.get("route_type") or record.get("route_type") or "cloud"),
                    available=bool(item.get("available", True)),
                    configured=bool(item.get("configured", True)),
                    base_url=item.get("base_url"),
                    detail=item.get("detail"),
                    is_default=(
                        provider == settings.default_provider and model == settings.default_model
                    ),
                )
            )
    return options


def _ollama_model_options(settings: Any) -> list[ModelOption]:
    base_url = settings.ollama_base_url.rstrip("/")
    default_provider = settings.default_provider
    default_model = settings.default_model
    try:
        names = _list_ollama_models(base_url)
    except RuntimeError as exc:
        return [
            ModelOption(
                provider="ollama",
                model=default_model,
                label=f"Ollama / {default_model}",
                route_type="local",
                available=False,
                configured=True,
                base_url=base_url,
                detail=str(exc),
                is_default=default_provider == "ollama",
            )
        ]
    if not names:
        return [
            ModelOption(
                provider="ollama",
                model=default_model,
                label=f"Ollama / {default_model}",
                route_type="local",
                available=False,
                configured=True,
                base_url=base_url,
                detail="Ollama is reachable but returned no installed models",
                is_default=default_provider == "ollama",
            )
        ]
    options = [
        ModelOption(
            provider="ollama",
            model=name,
            label=f"Ollama / {name}",
            route_type="local",
            available=True,
            configured=True,
            base_url=base_url,
            is_default=default_provider == "ollama" and name == default_model,
        )
        for name in names
    ]
    if default_provider == "ollama" and default_model not in names:
        options.append(
            ModelOption(
                provider="ollama",
                model=default_model,
                label=f"Ollama / {default_model}",
                route_type="local",
                available=False,
                configured=True,
                base_url=base_url,
                detail="Configured default model is not installed in Ollama",
                is_default=True,
            )
        )
    return options


def _list_ollama_models(base_url: str) -> list[str]:
    request = urllib.request.Request(f"{base_url}/api/tags", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = _read_http_error(exc)
        raise RuntimeError(
            f"Ollama model list failed: HTTP {exc.code}: {_sanitize_error_message(body)}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama is not reachable: {exc}") from exc
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []
    names = []
    for item in models:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            names.append(name)
    return sorted(set(names))


def _dedupe_model_options(options: list[ModelOption]) -> list[ModelOption]:
    deduped: dict[tuple[str, str, str | None], ModelOption] = {}
    for option in options:
        key = (option.provider, option.model, option.base_url)
        existing = deduped.get(key)
        if (
            existing is None
            or (option.is_default and not existing.is_default)
            or (option.available and not existing.available)
        ):
            deduped[key] = option
    return sorted(
        deduped.values(),
        key=lambda item: (
            not item.is_default,
            not item.available,
            item.provider,
            item.model,
        ),
    )


def _recommended_option(
    options: list[ModelOption],
    default_provider: str,
    default_model: str,
) -> ModelOption:
    for option in options:
        if (
            option.provider == default_provider
            and option.model == default_model
            and option.available
        ):
            return option
    provider_options = [
        option for option in options if option.available and option.provider == default_provider
    ]
    if provider_options:
        return min(provider_options, key=_model_preference_score)
    available = [option for option in options if option.available]
    if available:
        return min(available, key=_model_preference_score)
    return next((option for option in options if option.is_default), options[0])


def _model_preference_score(option: ModelOption) -> tuple[int, str]:
    if option.provider != "ollama":
        return (100, option.model)
    model = option.model.lower()
    if "embed" in model:
        return (900, model)
    for index, marker in enumerate(PREFERRED_OLLAMA_MODELS):
        if marker in model:
            return (index, model)
    if any(marker in model for marker in ("7b", "8b", "small", "mini", "nano")):
        return (50, model)
    if any(marker in model for marker in ("20b", "26b", "30b", "70b")):
        return (200, model)
    return (100, model)


def _map_litellm_error(provider: str, exc: Exception) -> RuntimeError:
    return _map_provider_error(provider, exc)


def _map_provider_error(provider: str, exc: Exception) -> RuntimeError:
    message = str(exc)
    lowered = message.lower()
    auth_markers = (
        "401",
        "403",
        "auth",
        "api key",
        "credential",
        "invalid_grant",
        "permission",
        "unauthorized",
    )
    if any(marker in lowered for marker in auth_markers):
        return ProviderAuthError(
            f"{provider} credential failed: {_sanitize_error_message(message)}"
        )
    return RuntimeError(f"{provider} model request failed: {_sanitize_error_message(message)}")


def _response_output_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str):
        return output_text
    if isinstance(response, dict):
        output_text = response.get("output_text")
        if isinstance(output_text, str):
            return output_text
        output = response.get("output")
    else:
        output = getattr(response, "output", None)
    parts: list[str] = []
    if isinstance(output, list):
        for item in output:
            content = (
                item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
            )
            if not isinstance(content, list):
                continue
            for part in content:
                text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts)


def _collect_response_stream(response: Any) -> tuple[str, Any]:
    if isinstance(response, dict) or isinstance(response, (str, bytes)):
        return "", response
    if not hasattr(response, "__iter__"):
        return "", response

    parts: list[str] = []
    final_response: Any = response
    for event in response:
        event_type = _event_value(event, "type")
        delta = _event_value(event, "delta")
        if (
            isinstance(event_type, str)
            and "output_text.delta" in event_type
            and isinstance(delta, str)
        ):
            parts.append(delta)
        if event_type == "response.completed":
            completed = _event_value(event, "response")
            if completed is not None:
                final_response = completed
    return "".join(parts), final_response


def _response_usage_tokens(response: Any, key: str) -> int:
    usage = (
        response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    )
    if usage is None:
        return 0
    value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _event_value(event: Any, key: str) -> Any:
    if isinstance(event, dict):
        return event.get(key)
    return getattr(event, key, None)


def _litellm_cost_usd(litellm_module: Any, response: Any) -> float:
    completion_cost = getattr(litellm_module, "completion_cost", None)
    if not callable(completion_cost):
        return 0.0
    try:
        cost = completion_cost(completion_response=response)
    except Exception:
        return 0.0
    try:
        return round(float(cost or 0.0), 8)
    except (TypeError, ValueError):
        return 0.0


def _sanitize_error_message(message: str) -> str:
    one_line = " ".join(message.split())
    if len(one_line) > 500:
        return one_line[:497].rstrip() + "..."
    return one_line


def _read_http_error(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8")
    except Exception:
        return str(exc)
