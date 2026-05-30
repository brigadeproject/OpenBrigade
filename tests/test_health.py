from __future__ import annotations

from brigade.config import Settings
from brigade.health import check_configured_datastores


def test_health_reports_unconfigured_datastores(tmp_path):
    settings = Settings(config_path=tmp_path / "config.json", data_dir=tmp_path)

    checks = check_configured_datastores(settings)

    assert {item.name for item in checks} == {"postgres", "redis", "qdrant", "neo4j"}
    assert not any(item.ok for item in checks)
    assert all(item.detail == "not configured" for item in checks)
