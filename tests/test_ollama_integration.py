from __future__ import annotations

import os

import pytest

from brigade.providers import OllamaProvider

pytestmark = pytest.mark.integration


def test_ollama_live_completion_when_enabled():
    base_url = os.environ.get("BRIGADE_TEST_OLLAMA_BASE_URL")
    model = os.environ.get("BRIGADE_TEST_OLLAMA_MODEL")
    if not base_url or not model:
        pytest.skip("set BRIGADE_TEST_OLLAMA_BASE_URL and BRIGADE_TEST_OLLAMA_MODEL to enable")

    response = OllamaProvider(base_url=base_url, model=model).complete("Reply with one short word.")

    assert response.provider == "ollama"
    assert response.model == model
    assert response.text.strip()
