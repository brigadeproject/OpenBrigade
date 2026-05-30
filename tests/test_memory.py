from __future__ import annotations

from brigade.memory import MAX_MEMORY_BYTES, append_daily_memory, curate_memory


def test_append_daily_memory_and_curate_with_soft_cap(tmp_path):
    daily = append_daily_memory(tmp_path, "20260517", "Observed a useful revenue idea")
    assert "Observed a useful revenue idea" in daily.read_text(encoding="utf-8")

    memory = curate_memory(tmp_path, [f"important note {index}" for index in range(200)])

    assert memory.exists()
    assert len(memory.read_bytes()) <= MAX_MEMORY_BYTES
    assert memory.read_text(encoding="utf-8").startswith("# Memory")
