from __future__ import annotations

import os


def pytest_configure() -> None:
    os.environ.setdefault("BRIGADE_ALLOW_JSON_STORE", "1")
