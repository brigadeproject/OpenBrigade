from __future__ import annotations

import pytest

from brigade.ingestion import chunk_text
from brigade.rbac import can
from brigade.schemas import Role, User


def test_chunk_text_overlaps_long_documents():
    chunks = chunk_text("abcdef" * 100, max_chars=100, overlap=10)

    assert len(chunks) > 1
    assert chunks[0].text[-10:] == chunks[1].text[:10]


def test_chunk_text_rejects_invalid_overlap():
    with pytest.raises(ValueError, match="max_chars"):
        chunk_text("abc", max_chars=10, overlap=10)


def test_rbac_permissions():
    owner = User("owner", Role.OWNER)
    operator = User("op", Role.OPERATOR)
    observer = User("obs", Role.OBSERVER)

    assert can(owner, "mission:write")
    assert can(operator, "task:write")
    assert not can(observer, "task:write")
