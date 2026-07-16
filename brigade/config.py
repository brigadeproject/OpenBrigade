from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SUPPORTED_MODEL_PROVIDERS = {
    "ollama",
    "litellm",
    "openai",
    "openai-codex",
    "anthropic",
    "gemini",
}
SUPPORTED_AUTH_MODES = {"api_key", "oauth"}


@dataclass(frozen=True)
class Settings:
    config_path: Path
    data_dir: Path
    log_level: str = "INFO"
    orchestrator_cadence_seconds: int = 900
    stale_work_seconds: int = 86_400
    hung_task_seconds: int = 1800
    auto_recover_enabled: bool = True
    max_auto_reissue: int = 2
    duplicate_reconciliation_enabled: bool = True
    proactive_mode: str = "create"
    proactive_creation_enabled: bool = True
    max_proactive_proposals_per_cycle: int = 1
    max_proactive_creations_per_cycle: int = 1
    intake_mode: str = "propose"
    max_intake_assignments_per_cycle: int = 2
    intake_route_chief: str | None = None
    intake_default_priority: str = "normal"
    rest_enabled: bool = True
    rest_window_start_utc: str = "03:00"
    rest_window_end_utc: str = "05:00"
    rest_idle_cycles_threshold: int = 6
    rest_min_interval_seconds: int = 86_400
    blocker_resolution_enabled: bool = True
    dispatch_starvation_alert_cycles: int = 4
    recurrence_detection_threshold: int = 3
    recurrence_lookback_days: int = 14
    postgres_dsn: str | None = None
    redis_url: str | None = None
    qdrant_url: str | None = None
    qdrant_collection: str = "brigade_episodes"
    ollama_embedding_base_url: str | None = None
    ollama_embedding_model: str = "nomic-embed-text:latest"
    ollama_embedding_vector_size: int = 768
    neo4j_uri: str | None = None
    neo4j_http_url: str | None = None
    neo4j_auth: str | None = None
    jwt_secret: str = "openbrigade-dev-secret"
    jwt_issuer: str = "openbrigade-local"
    jwt_audience: str = "openbrigade"
    require_auth: bool = False
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    default_provider: str = "ollama"
    default_model: str = "qwen2.5-coder:7b"
    ollama_base_url: str = "http://127.0.0.1:11434"
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    secret_store_path: Path | None = None
    openai_auth_mode: str = "api_key"
    openai_codex_auth_mode: str = "api_key"
    gemini_auth_mode: str = "api_key"
    telegram_bot_token: str | None = None
    telegram_webhook_enabled: bool = False
    telegram_webhook_secret: str | None = None
    telegram_default_agent: str = "sage"
    telegram_allowlist: str | None = None
    operator_telegram_chat_id: str | None = None
    google_chat_webhook_enabled: bool = False
    google_chat_secret: str | None = None
    google_chat_default_agent: str = "sage"
    google_chat_allowlist: str | None = None
    connector_rate_limit_count: int = 20
    connector_rate_limit_window_seconds: int = 60
    connector_max_inbound_chars: int = 4000
    connector_max_outbound_chars: int = 3500
    connector_max_body_bytes: int = 1_048_576
    chief_chat_enabled: bool = True
    chief_chat_max_iterations: int = 6
    chief_chat_history_window: int = 12
    chief_chat_default_persona: str = "auto"
    chief_chat_web_fetch_enabled: bool = True
    connector_chief_chat_enabled: bool = False
    chief_chat_connector_max_iterations: int = 3
    allow_json_store: bool = False


def load_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _env(name: str, dotenv: dict[str, str], default: str | None = None) -> str | None:
    return os.environ.get(name, dotenv.get(name, default))


def _env_bool(
    name: str,
    dotenv: dict[str, str],
    default: bool | str | None = None,
) -> bool:
    raw_default = str(default) if default is not None else "false"
    return (_env(name, dotenv, raw_default) or "false").lower() in {"1", "true", "yes", "on"}


def _env_int(
    name: str,
    dotenv: dict[str, str],
    default: int | str,
) -> int:
    return int(_env(name, dotenv, str(default)) or default)


def _compose_host(dotenv: dict[str, str], config: dict[str, Any]) -> str:
    return (
        _env("BRIGADE_HOST", dotenv, config.get("host"))
        or _env("BRIGADE_BIND_ADDRESS", dotenv, config.get("bind_address", "127.0.0.1"))
        or "127.0.0.1"
    )


def load_settings(
    config_path: Path | str = "brigade.config.json",
    env_path: Path | str = ".env",
) -> Settings:
    config_file = Path(config_path)
    dotenv = load_dotenv(Path(env_path))
    config = _load_json(config_file)

    data_dir = Path(
        _env("BRIGADE_DATA_DIR", dotenv, config.get("data_dir", ".brigade")) or ".brigade"
    )
    cadence_raw = _env(
        "BRIGADE_ORCHESTRATOR_CADENCE_SECONDS",
        dotenv,
        str(config.get("orchestrator_cadence_seconds", 900)),
    )
    host = _compose_host(dotenv, config)
    postgres_dsn = _env("BRIGADE_POSTGRES_DSN", dotenv, config.get("postgres_dsn"))
    if not postgres_dsn:
        postgres_user = _env(
            "BRIGADE_POSTGRES_USER",
            dotenv,
            config.get("postgres_user", "brigade"),
        )
        postgres_password = _env(
            "BRIGADE_POSTGRES_PASSWORD",
            dotenv,
            config.get("postgres_password"),
        )
        postgres_db = _env("BRIGADE_POSTGRES_DB", dotenv, config.get("postgres_db", "brigade"))
        postgres_port = _env(
            "BRIGADE_POSTGRES_PORT",
            dotenv,
            str(config.get("postgres_port", 55432)),
        )
        if postgres_user and postgres_password and postgres_db and postgres_port:
            postgres_dsn = (
                f"postgresql://{postgres_user}:{postgres_password}@{host}:{postgres_port}/{postgres_db}"
            )

    redis_url = _env("BRIGADE_REDIS_URL", dotenv, config.get("redis_url"))
    if not redis_url:
        redis_port = _env("BRIGADE_REDIS_PORT", dotenv, str(config.get("redis_port", 56379)))
        if redis_port:
            redis_url = f"redis://{host}:{redis_port}/0"

    qdrant_url = _env("BRIGADE_QDRANT_URL", dotenv, config.get("qdrant_url"))
    if not qdrant_url:
        qdrant_port = _env(
            "BRIGADE_QDRANT_HTTP_PORT",
            dotenv,
            str(config.get("qdrant_http_port", 56333)),
        )
        if qdrant_port:
            qdrant_url = f"http://{host}:{qdrant_port}"

    neo4j_uri = _env("BRIGADE_NEO4J_URI", dotenv, config.get("neo4j_uri"))
    if not neo4j_uri:
        neo4j_port = _env(
            "BRIGADE_NEO4J_BOLT_PORT",
            dotenv,
            str(config.get("neo4j_bolt_port", 57687)),
        )
        if neo4j_port:
            neo4j_uri = f"bolt://{host}:{neo4j_port}"
    neo4j_http_url = _env("BRIGADE_NEO4J_HTTP_URL", dotenv, config.get("neo4j_http_url"))
    if not neo4j_http_url:
        neo4j_http_port = _env(
            "BRIGADE_NEO4J_HTTP_PORT",
            dotenv,
            str(config.get("neo4j_http_port", 57474)),
        )
        if neo4j_http_port:
            neo4j_http_url = f"http://{host}:{neo4j_http_port}"
    neo4j_auth = _env("BRIGADE_NEO4J_AUTH", dotenv, config.get("neo4j_auth"))
    web_port_raw = _env("BRIGADE_WEB_PORT", dotenv, str(config.get("web_port", 8080)))
    ollama_base_url = _env(
        "BRIGADE_OLLAMA_BASE_URL",
        dotenv,
        config.get("ollama_base_url", "http://127.0.0.1:11434"),
    ) or "http://127.0.0.1:11434"
    default_provider = _env(
        "BRIGADE_DEFAULT_PROVIDER",
        dotenv,
        config.get("default_provider"),
    )
    if not default_provider:
        default_provider = "ollama"
    if default_provider == "fake":
        default_provider = "ollama"
    if default_provider not in SUPPORTED_MODEL_PROVIDERS:
        raise ValueError(
            f"unsupported default model provider '{default_provider}'; choose one of "
            f"{', '.join(sorted(SUPPORTED_MODEL_PROVIDERS))}"
        )
    openai_auth_mode = (
        _env("BRIGADE_OPENAI_AUTH_MODE", dotenv, config.get("openai_auth_mode", "api_key"))
        or "api_key"
    )
    openai_codex_auth_mode = (
        _env(
            "BRIGADE_OPENAI_CODEX_AUTH_MODE",
            dotenv,
            config.get("openai_codex_auth_mode", config.get("openai_auth_mode", "api_key")),
        )
        or "api_key"
    )
    gemini_auth_mode = (
        _env("BRIGADE_GEMINI_AUTH_MODE", dotenv, config.get("gemini_auth_mode", "api_key"))
        or "api_key"
    )
    for key, auth_mode in (
        ("BRIGADE_OPENAI_AUTH_MODE", openai_auth_mode),
        ("BRIGADE_OPENAI_CODEX_AUTH_MODE", openai_codex_auth_mode),
        ("BRIGADE_GEMINI_AUTH_MODE", gemini_auth_mode),
    ):
        if auth_mode not in SUPPORTED_AUTH_MODES:
            raise ValueError(
                f"unsupported auth mode for {key}: {auth_mode}; choose one of "
                f"{', '.join(sorted(SUPPORTED_AUTH_MODES))}"
            )

    return Settings(
        config_path=config_file,
        data_dir=data_dir,
        log_level=_env("BRIGADE_LOG_LEVEL", dotenv, config.get("log_level", "INFO")) or "INFO",
        orchestrator_cadence_seconds=int(cadence_raw or 900),
        stale_work_seconds=_env_int(
            "BRIGADE_STALE_WORK_SECONDS",
            dotenv,
            config.get("stale_work_seconds", 86_400),
        ),
        hung_task_seconds=_env_int(
            "BRIGADE_HUNG_TASK_SECONDS",
            dotenv,
            config.get("hung_task_seconds", 1800),
        ),
        auto_recover_enabled=_env_bool(
            "BRIGADE_AUTO_RECOVER_ENABLED",
            dotenv,
            config.get("auto_recover_enabled", True),
        ),
        max_auto_reissue=_env_int(
            "BRIGADE_MAX_AUTO_REISSUE",
            dotenv,
            config.get("max_auto_reissue", 2),
        ),
        duplicate_reconciliation_enabled=_env_bool(
            "BRIGADE_DUPLICATE_RECONCILIATION_ENABLED",
            dotenv,
            config.get("duplicate_reconciliation_enabled", True),
        ),
        proactive_mode=(
            _env("BRIGADE_PROACTIVE_MODE", dotenv, config.get("proactive_mode", "create"))
            or "create"
        ),
        proactive_creation_enabled=_env_bool(
            "BRIGADE_PROACTIVE_CREATION_ENABLED",
            dotenv,
            config.get("proactive_creation_enabled", True),
        ),
        max_proactive_proposals_per_cycle=_env_int(
            "BRIGADE_MAX_PROACTIVE_PROPOSALS_PER_CYCLE",
            dotenv,
            config.get("max_proactive_proposals_per_cycle", 1),
        ),
        max_proactive_creations_per_cycle=_env_int(
            "BRIGADE_MAX_PROACTIVE_CREATIONS_PER_CYCLE",
            dotenv,
            config.get("max_proactive_creations_per_cycle", 1),
        ),
        intake_mode=(
            _env("BRIGADE_INTAKE_MODE", dotenv, config.get("intake_mode", "propose"))
            or "propose"
        ),
        max_intake_assignments_per_cycle=_env_int(
            "BRIGADE_MAX_INTAKE_ASSIGNMENTS_PER_CYCLE",
            dotenv,
            config.get("max_intake_assignments_per_cycle", 2),
        ),
        intake_route_chief=_env(
            "BRIGADE_INTAKE_ROUTE_CHIEF",
            dotenv,
            config.get("intake_route_chief"),
        ),
        intake_default_priority=(
            _env(
                "BRIGADE_INTAKE_DEFAULT_PRIORITY",
                dotenv,
                config.get("intake_default_priority", "normal"),
            )
            or "normal"
        ),
        rest_enabled=_env_bool(
            "BRIGADE_REST_ENABLED",
            dotenv,
            config.get("rest_enabled", True),
        ),
        rest_window_start_utc=(
            _env(
                "BRIGADE_REST_WINDOW_START_UTC",
                dotenv,
                config.get("rest_window_start_utc", "03:00"),
            )
            or "03:00"
        ),
        rest_window_end_utc=(
            _env(
                "BRIGADE_REST_WINDOW_END_UTC",
                dotenv,
                config.get("rest_window_end_utc", "05:00"),
            )
            or "05:00"
        ),
        rest_idle_cycles_threshold=_env_int(
            "BRIGADE_REST_IDLE_CYCLES_THRESHOLD",
            dotenv,
            config.get("rest_idle_cycles_threshold", 6),
        ),
        rest_min_interval_seconds=_env_int(
            "BRIGADE_REST_MIN_INTERVAL_SECONDS",
            dotenv,
            config.get("rest_min_interval_seconds", 86_400),
        ),
        blocker_resolution_enabled=_env_bool(
            "BRIGADE_BLOCKER_RESOLUTION_ENABLED",
            dotenv,
            config.get("blocker_resolution_enabled", True),
        ),
        dispatch_starvation_alert_cycles=_env_int(
            "BRIGADE_DISPATCH_STARVATION_ALERT_CYCLES",
            dotenv,
            config.get("dispatch_starvation_alert_cycles", 4),
        ),
        recurrence_detection_threshold=_env_int(
            "BRIGADE_RECURRENCE_DETECTION_THRESHOLD",
            dotenv,
            config.get("recurrence_detection_threshold", 3),
        ),
        recurrence_lookback_days=_env_int(
            "BRIGADE_RECURRENCE_LOOKBACK_DAYS",
            dotenv,
            config.get("recurrence_lookback_days", 14),
        ),
        postgres_dsn=postgres_dsn,
        redis_url=redis_url,
        qdrant_url=qdrant_url,
        qdrant_collection=_env(
            "BRIGADE_QDRANT_COLLECTION",
            dotenv,
            config.get("qdrant_collection", "brigade_episodes"),
        )
        or "brigade_episodes",
        ollama_embedding_base_url=_env(
            "BRIGADE_OLLAMA_EMBEDDING_BASE_URL",
            dotenv,
            config.get("ollama_embedding_base_url"),
        ),
        ollama_embedding_model=_env(
            "BRIGADE_OLLAMA_EMBEDDING_MODEL",
            dotenv,
            config.get("ollama_embedding_model", "nomic-embed-text:latest"),
        )
        or "nomic-embed-text:latest",
        ollama_embedding_vector_size=_env_int(
            "BRIGADE_OLLAMA_EMBEDDING_VECTOR_SIZE",
            dotenv,
            config.get("ollama_embedding_vector_size", 768),
        ),
        neo4j_uri=neo4j_uri,
        neo4j_http_url=neo4j_http_url,
        neo4j_auth=neo4j_auth,
        jwt_secret=_env(
            "BRIGADE_JWT_SECRET",
            dotenv,
            config.get("jwt_secret", "openbrigade-dev-secret"),
        )
        or "openbrigade-dev-secret",
        jwt_issuer=_env(
            "BRIGADE_JWT_ISSUER",
            dotenv,
            config.get("jwt_issuer", "openbrigade-local"),
        )
        or "openbrigade-local",
        jwt_audience=_env(
            "BRIGADE_JWT_AUDIENCE",
            dotenv,
            config.get("jwt_audience", "openbrigade"),
        )
        or "openbrigade",
        require_auth=(
            (
                _env(
                    "BRIGADE_REQUIRE_AUTH",
                    dotenv,
                    str(config.get("require_auth", False)),
                )
                or "false"
            ).lower()
            in {"1", "true", "yes", "on"}
        ),
        web_host=_env("BRIGADE_WEB_HOST", dotenv, config.get("web_host", "0.0.0.0"))
        or "0.0.0.0",
        web_port=int(web_port_raw or 8080),
        default_provider=default_provider,
        default_model=_env(
            "BRIGADE_DEFAULT_MODEL",
            dotenv,
            config.get("default_model", "qwen2.5-coder:7b"),
        )
        or "qwen2.5-coder:7b",
        ollama_base_url=ollama_base_url,
        openai_api_key=_env("OPENAI_API_KEY", dotenv, config.get("openai_api_key")),
        anthropic_api_key=_env("ANTHROPIC_API_KEY", dotenv, config.get("anthropic_api_key")),
        gemini_api_key=_env("GEMINI_API_KEY", dotenv, config.get("gemini_api_key")),
        secret_store_path=Path(
            _env(
                "BRIGADE_SECRET_STORE_PATH",
                dotenv,
                config.get("secret_store_path", str(data_dir / "secrets")),
            )
            or str(data_dir / "secrets")
        ),
        openai_auth_mode=openai_auth_mode,
        openai_codex_auth_mode=openai_codex_auth_mode,
        gemini_auth_mode=gemini_auth_mode,
        telegram_bot_token=_env(
            "BRIGADE_TELEGRAM_BOT_TOKEN",
            dotenv,
            config.get("telegram_bot_token"),
        ),
        telegram_webhook_enabled=_env_bool(
            "BRIGADE_TELEGRAM_WEBHOOK_ENABLED",
            dotenv,
            config.get("telegram_webhook_enabled", False),
        ),
        telegram_webhook_secret=_env(
            "BRIGADE_TELEGRAM_WEBHOOK_SECRET",
            dotenv,
            config.get("telegram_webhook_secret"),
        ),
        telegram_default_agent=_env(
            "BRIGADE_TELEGRAM_DEFAULT_AGENT",
            dotenv,
            config.get("telegram_default_agent", "sage"),
        )
        or "sage",
        telegram_allowlist=_env(
            "BRIGADE_TELEGRAM_ALLOWLIST",
            dotenv,
            config.get("telegram_allowlist"),
        ),
        operator_telegram_chat_id=_env(
            "BRIGADE_OPERATOR_TELEGRAM_CHAT_ID",
            dotenv,
            config.get("operator_telegram_chat_id"),
        ),
        google_chat_webhook_enabled=_env_bool(
            "BRIGADE_GOOGLE_CHAT_WEBHOOK_ENABLED",
            dotenv,
            config.get("google_chat_webhook_enabled", False),
        ),
        google_chat_secret=_env(
            "BRIGADE_GOOGLE_CHAT_SECRET",
            dotenv,
            config.get("google_chat_secret"),
        ),
        google_chat_default_agent=_env(
            "BRIGADE_GOOGLE_CHAT_DEFAULT_AGENT",
            dotenv,
            config.get("google_chat_default_agent", "sage"),
        )
        or "sage",
        google_chat_allowlist=_env(
            "BRIGADE_GOOGLE_CHAT_ALLOWLIST",
            dotenv,
            config.get("google_chat_allowlist"),
        ),
        connector_rate_limit_count=_env_int(
            "BRIGADE_CONNECTOR_RATE_LIMIT_COUNT",
            dotenv,
            config.get("connector_rate_limit_count", 20),
        ),
        connector_rate_limit_window_seconds=_env_int(
            "BRIGADE_CONNECTOR_RATE_LIMIT_WINDOW_SECONDS",
            dotenv,
            config.get("connector_rate_limit_window_seconds", 60),
        ),
        connector_max_inbound_chars=_env_int(
            "BRIGADE_CONNECTOR_MAX_INBOUND_CHARS",
            dotenv,
            config.get("connector_max_inbound_chars", 4000),
        ),
        connector_max_outbound_chars=_env_int(
            "BRIGADE_CONNECTOR_MAX_OUTBOUND_CHARS",
            dotenv,
            config.get("connector_max_outbound_chars", 3500),
        ),
        connector_max_body_bytes=_env_int(
            "BRIGADE_CONNECTOR_MAX_BODY_BYTES",
            dotenv,
            config.get("connector_max_body_bytes", 1_048_576),
        ),
        chief_chat_enabled=_env_bool(
            "BRIGADE_CHIEF_CHAT_ENABLED",
            dotenv,
            config.get("chief_chat_enabled", True),
        ),
        chief_chat_max_iterations=_env_int(
            "BRIGADE_CHIEF_CHAT_MAX_ITERATIONS",
            dotenv,
            config.get("chief_chat_max_iterations", 6),
        ),
        chief_chat_history_window=_env_int(
            "BRIGADE_CHIEF_CHAT_HISTORY_WINDOW",
            dotenv,
            config.get("chief_chat_history_window", 12),
        ),
        chief_chat_default_persona=_env(
            "BRIGADE_CHIEF_CHAT_DEFAULT_PERSONA",
            dotenv,
            config.get("chief_chat_default_persona", "auto"),
        )
        or "auto",
        chief_chat_web_fetch_enabled=_env_bool(
            "BRIGADE_CHIEF_CHAT_WEB_FETCH_ENABLED",
            dotenv,
            config.get("chief_chat_web_fetch_enabled", True),
        ),
        connector_chief_chat_enabled=_env_bool(
            "BRIGADE_CONNECTOR_CHIEF_CHAT_ENABLED",
            dotenv,
            config.get("connector_chief_chat_enabled", False),
        ),
        chief_chat_connector_max_iterations=_env_int(
            "BRIGADE_CHIEF_CHAT_CONNECTOR_MAX_ITERATIONS",
            dotenv,
            config.get("chief_chat_connector_max_iterations", 3),
        ),
        allow_json_store=_env_bool(
            "BRIGADE_ALLOW_JSON_STORE",
            dotenv,
            config.get("allow_json_store", False),
        ),
    )
