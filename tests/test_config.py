from __future__ import annotations

import json

import pytest

from brigade.config import load_settings


def test_load_settings_prefers_environment_over_json(tmp_path, monkeypatch):
    config = tmp_path / "brigade.config.json"
    config.write_text(
        json.dumps({"data_dir": "from-json", "log_level": "WARNING"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BRIGADE_LOG_LEVEL", "DEBUG")

    settings = load_settings(config_path=config, env_path=tmp_path / ".env")

    assert settings.data_dir.name == "from-json"
    assert settings.log_level == "DEBUG"


def test_load_settings_reads_dotenv(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "BRIGADE_ORCHESTRATOR_CADENCE_SECONDS=30\n"
        "BRIGADE_STALE_WORK_SECONDS=3600\n",
        encoding="utf-8",
    )

    settings = load_settings(config_path=tmp_path / "missing.json", env_path=env)

    assert settings.orchestrator_cadence_seconds == 30
    assert settings.stale_work_seconds == 3600
    # Proactive create-mode is the default as of 1.0.2 (see load_settings fallback).
    assert settings.proactive_mode == "create"
    assert settings.proactive_creation_enabled is True


def test_load_settings_reads_proactive_controls(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                "BRIGADE_PROACTIVE_MODE=create",
                "BRIGADE_PROACTIVE_CREATION_ENABLED=true",
                "BRIGADE_MAX_PROACTIVE_PROPOSALS_PER_CYCLE=2",
                "BRIGADE_MAX_PROACTIVE_CREATIONS_PER_CYCLE=1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    settings = load_settings(config_path=tmp_path / "missing.json", env_path=env)

    assert settings.proactive_mode == "create"
    assert settings.proactive_creation_enabled is True
    assert settings.max_proactive_proposals_per_cycle == 2
    assert settings.max_proactive_creations_per_cycle == 1


def test_load_settings_uses_configured_ollama_as_live_default(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "BRIGADE_OLLAMA_BASE_URL=http://host.docker.internal:11434\n"
        "BRIGADE_DEFAULT_MODEL=gpt-oss:20b\n",
        encoding="utf-8",
    )

    settings = load_settings(config_path=tmp_path / "missing.json", env_path=env)

    assert settings.default_provider == "ollama"
    assert settings.ollama_base_url == "http://host.docker.internal:11434"
    assert settings.default_model == "gpt-oss:20b"


def test_load_settings_can_use_openai_codex_default(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "BRIGADE_DEFAULT_PROVIDER=openai-codex\n"
        "BRIGADE_DEFAULT_MODEL=gpt-5.3-codex-spark\n"
        "BRIGADE_OPENAI_CODEX_AUTH_MODE=api_key\n",
        encoding="utf-8",
    )

    settings = load_settings(config_path=tmp_path / "missing.json", env_path=env)

    assert settings.default_provider == "openai-codex"
    assert settings.default_model == "gpt-5.3-codex-spark"
    assert settings.openai_codex_auth_mode == "api_key"


def test_load_settings_rejects_unknown_default_provider(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BRIGADE_DEFAULT_PROVIDER=codex\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported default model provider"):
        load_settings(config_path=tmp_path / "missing.json", env_path=env)


def test_load_settings_rejects_unknown_auth_mode(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "BRIGADE_DEFAULT_PROVIDER=openai-codex\n"
        "BRIGADE_OPENAI_CODEX_AUTH_MODE=ollama\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported auth mode"):
        load_settings(config_path=tmp_path / "missing.json", env_path=env)


def test_load_settings_retired_provider_falls_back_to_ollama(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                "BRIGADE_OLLAMA_BASE_URL=http://host.docker.internal:11434",
                "BRIGADE_DEFAULT_PROVIDER=fake",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    settings = load_settings(config_path=tmp_path / "missing.json", env_path=env)

    assert settings.default_provider == "ollama"


def test_load_settings_derives_host_datastore_urls_from_compose_env(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                "BRIGADE_BIND_ADDRESS=127.0.0.1",
                "BRIGADE_POSTGRES_DB=brigadepg",
                "BRIGADE_POSTGRES_USER=brigade",
                "BRIGADE_POSTGRES_PASSWORD=secretpass",
                "BRIGADE_POSTGRES_PORT=55432",
                "BRIGADE_REDIS_PORT=56379",
                "BRIGADE_QDRANT_HTTP_PORT=56333",
                "BRIGADE_QDRANT_COLLECTION=brigade_episodes_nomic_embed_text",
                "BRIGADE_OLLAMA_EMBEDDING_BASE_URL=http://host.docker.internal:11434",
                "BRIGADE_OLLAMA_EMBEDDING_MODEL=nomic-embed-text:latest",
                "BRIGADE_OLLAMA_EMBEDDING_VECTOR_SIZE=768",
                "BRIGADE_NEO4J_BOLT_PORT=57687",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    settings = load_settings(config_path=tmp_path / "missing.json", env_path=env)

    assert settings.postgres_dsn == "postgresql://brigade:secretpass@127.0.0.1:55432/brigadepg"
    assert settings.redis_url == "redis://127.0.0.1:56379/0"
    assert settings.qdrant_url == "http://127.0.0.1:56333"
    assert settings.qdrant_collection == "brigade_episodes_nomic_embed_text"
    assert settings.ollama_embedding_base_url == "http://host.docker.internal:11434"
    assert settings.ollama_embedding_model == "nomic-embed-text:latest"
    assert settings.ollama_embedding_vector_size == 768
    assert settings.neo4j_uri == "bolt://127.0.0.1:57687"


def test_load_settings_reads_external_connection_flags(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                "BRIGADE_TELEGRAM_WEBHOOK_ENABLED=true",
                "BRIGADE_TELEGRAM_WEBHOOK_SECRET=telegram-secret",
                "BRIGADE_TELEGRAM_DEFAULT_AGENT=sage",
                "BRIGADE_GOOGLE_CHAT_WEBHOOK_ENABLED=1",
                "BRIGADE_GOOGLE_CHAT_SECRET=chat-secret",
                "BRIGADE_GOOGLE_CHAT_DEFAULT_AGENT=scout",
                "BRIGADE_OPENAI_AUTH_MODE=oauth",
                "BRIGADE_GEMINI_AUTH_MODE=oauth",
                "BRIGADE_CONNECTOR_RATE_LIMIT_COUNT=3",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    settings = load_settings(config_path=tmp_path / "missing.json", env_path=env)

    assert settings.telegram_webhook_enabled is True
    assert settings.telegram_webhook_secret == "telegram-secret"
    assert settings.telegram_default_agent == "sage"
    assert settings.google_chat_webhook_enabled is True
    assert settings.google_chat_secret == "chat-secret"
    assert settings.google_chat_default_agent == "scout"
    assert settings.openai_auth_mode == "oauth"
    assert settings.gemini_auth_mode == "oauth"
    assert settings.connector_rate_limit_count == 3
