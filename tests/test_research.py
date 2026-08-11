from __future__ import annotations

import json

import brigade.research as research


def test_search_with_retry_retries_transient_failure(monkeypatch):
    calls = 0
    monkeypatch.setattr(research.time, "sleep", lambda seconds: None)

    def search(query):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("temporary")
        return [], "https://search.example/result"

    assert research.search_with_retry("test", limit=1, search=search) == ([], "https://search.example/result")
    assert calls == 2


def test_searxng_search_sends_only_configured_engines(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            return json.dumps({"results": []}).encode()

        def geturl(self):
            return "http://search.local/search"

    monkeypatch.setenv("BRIGADE_SEARXNG_URL", "http://search.local")
    monkeypatch.setenv("BRIGADE_SEARCH_ALLOWED_ENGINES", "bing, arxiv")
    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        return Response()

    monkeypatch.setattr(research.urllib.request, "urlopen", fake_urlopen)

    research.searxng_search("statute", limit=1)

    assert "engines=bing%2Carxiv" in captured["url"]
