from __future__ import annotations

import os

import pytest

from brigade.config import Settings
from brigade.providers import provider_from_settings


@pytest.mark.integration
def test_live_openai_smoke_requires_explicit_env(tmp_path):
    if os.environ.get("BRIGADE_LIVE_OPENAI_SMOKE") != "1":
        pytest.skip("set BRIGADE_LIVE_OPENAI_SMOKE=1 to run live OpenAI smoke")
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY is required for live OpenAI smoke")

    provider = provider_from_settings(
        Settings(config_path=tmp_path / "config.json", data_dir=tmp_path),
        provider="openai",
        model=os.environ.get("BRIGADE_LIVE_OPENAI_MODEL", "gpt-4.1-mini"),
    )
    response = provider.complete("Reply with exactly: ok")

    assert response.text.strip()
    assert response.input_tokens >= 0
    assert response.output_tokens >= 0


@pytest.mark.integration
def test_live_gemini_smoke_requires_explicit_env(tmp_path):
    if os.environ.get("BRIGADE_LIVE_GEMINI_SMOKE") != "1":
        pytest.skip("set BRIGADE_LIVE_GEMINI_SMOKE=1 to run live Gemini smoke")
    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY is required for live Gemini smoke")

    provider = provider_from_settings(
        Settings(config_path=tmp_path / "config.json", data_dir=tmp_path),
        provider="gemini",
        model=os.environ.get("BRIGADE_LIVE_GEMINI_MODEL", "gemini-1.5-flash"),
    )
    response = provider.complete("Reply with exactly: ok")

    assert response.text.strip()
    assert response.input_tokens >= 0
    assert response.output_tokens >= 0
