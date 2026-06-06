from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from brigade.secrets import oauth_credential_expired, read_oauth_credential

PREFERRED_OLLAMA_MODELS = (
    "qwen2.5-coder:7b",
    "qwen2.5:7b",
    "llama3.1:8b",
    "llama3:8b",
    "mistral:7b",
    "devstral-small",
)
RETIRED_MODEL_PROVIDERS = {"fake"}


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

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "gpt-oss:20b",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

    def complete(self, prompt: str) -> ModelResponse:
        payload = json.dumps(
            {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/generate",
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

        text = str(data.get("response", ""))
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
    if provider_name == "ollama":
        return OllamaProvider(base_url=api_base or settings.ollama_base_url, model=model_name)
    if provider_name in {"openai", "openai-codex"}:
        auth_mode = (
            settings.openai_codex_auth_mode
            if provider_name == "openai-codex"
            else settings.openai_auth_mode
        )
        return LiteLLMProvider(
            model=model_name,
            api_key=api_key or settings.openai_api_key,
            api_base=api_base,
            provider_name=provider_name,
            auth_mode=auth_mode,
            oauth_credential=read_oauth_credential(settings, provider_name)
            if auth_mode == "oauth"
            else None,
        )
    if provider_name == "anthropic":
        return LiteLLMProvider(
            model=model_name,
            api_key=api_key or settings.anthropic_api_key,
            api_base=api_base,
            provider_name="anthropic",
        )
    if provider_name == "gemini":
        model_name = model_name if model_name.startswith("gemini/") else f"gemini/{model_name}"
        return LiteLLMProvider(
            model=model_name,
            api_key=api_key or settings.gemini_api_key,
            api_base=api_base,
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


def available_model_options(settings: Any) -> dict[str, Any]:
    default_provider = settings.default_provider
    default_model = settings.default_model
    options: list[ModelOption] = []
    options.extend(_ollama_model_options(settings))

    if settings.openai_api_key or settings.openai_auth_mode == "oauth" or default_provider in {
        "openai",
        "openai-codex",
    }:
        for provider_name in ("openai", "openai-codex"):
            configured = bool(settings.openai_api_key) or (
                getattr(settings, "openai_auth_mode", "api_key") == "oauth"
            )
            model = default_model if default_provider == provider_name else "gpt-4.1-mini"
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
        model = default_model if default_provider == "anthropic" else "claude-3-5-haiku-latest"
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
    }


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
        if existing is None or (option.is_default and not existing.is_default):
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
