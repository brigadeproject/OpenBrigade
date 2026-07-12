"""The /mobile page route serves the mobile companion SPA."""

from __future__ import annotations

import pytest

from brigade.config import Settings
from brigade.state import JsonStateStore


def _app(tmp_path, monkeypatch, static_root):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    import brigade.web as web

    monkeypatch.setattr(web, "_static_root", lambda candidates=None: static_root)
    store = JsonStateStore(tmp_path / "state.json")
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
    )
    return web.create_app(settings, store)


def test_mobile_serves_built_page(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    static_root = tmp_path / "dist"
    (static_root / "assets").mkdir(parents=True)
    (static_root / "index.html").write_text("<html>desktop</html>", encoding="utf-8")
    (static_root / "mobile.html").write_text(
        "<html><title>OpenBrigade Mobile</title></html>", encoding="utf-8"
    )
    client = TestClient(_app(tmp_path, monkeypatch, static_root))

    for path in ("/mobile", "/mobile.html"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert "OpenBrigade Mobile" in resp.text


def test_mobile_falls_back_without_build(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    client = TestClient(_app(tmp_path, monkeypatch, tmp_path / "missing-dist"))
    resp = client.get("/mobile")
    assert resp.status_code == 200
    assert resp.text  # graceful fallback page, not a 404
