from __future__ import annotations

import json

import pytest

from brigade.schemas import Agent, Assignment
from brigade.workspace import (
    ASSIGNMENT_MARKER,
    REQUIRED_AGENT_FILES,
    HeartbeatValidationError,
    parse_heartbeat_assignment_block,
    write_heartbeat_assignment,
)


def test_write_heartbeat_assignment_preserves_parseable_block(tmp_path):
    agent = Agent(
        agent_id="sage",
        display_name="SAGE",
        workspace_path="workspace-sage",
        role="crew_chief",
    )
    assignment = Assignment(
        assignment="Draft a revenue experiment",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )

    heartbeat = write_heartbeat_assignment(agent, assignment, tmp_path)

    assert heartbeat.name == "HEARTBEAT.md"
    text = heartbeat.read_text(encoding="utf-8")
    assert ASSIGNMENT_MARKER in text
    payload = text.split(ASSIGNMENT_MARKER, 1)[1].split("```", 1)[0]
    assert json.loads(payload)["assignment"] == "Draft a revenue experiment"
    for filename in REQUIRED_AGENT_FILES:
        assert (tmp_path / "workspace-sage" / filename).exists()


def test_parse_heartbeat_assignment_block_returns_assignment_metadata():
    assignment = Assignment(
        assignment="Draft a revenue experiment",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    payload = json.dumps(assignment.to_dict(), indent=2, sort_keys=True)

    block = parse_heartbeat_assignment_block(
        f"# Heartbeat\n\n{ASSIGNMENT_MARKER}\n{payload}\n```\n"
    )

    assert block.assignment.assignment == "Draft a revenue experiment"
    assert block.assignment_id == assignment.assignment_id
    assert block.assigned_to == "sage"


def test_parse_heartbeat_assignment_block_rejects_invalid_json():
    with pytest.raises(HeartbeatValidationError) as excinfo:
        parse_heartbeat_assignment_block(f"{ASSIGNMENT_MARKER}\n{{invalid json}}\n```")

    assert excinfo.value.code == "invalid_json"


def test_parse_heartbeat_assignment_block_rejects_duplicate_blocks():
    assignment = Assignment(
        assignment="Draft a revenue experiment",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    payload = json.dumps(assignment.to_dict(), indent=2, sort_keys=True)

    with pytest.raises(HeartbeatValidationError) as excinfo:
        parse_heartbeat_assignment_block(
            f"{ASSIGNMENT_MARKER}\n{payload}\n```\n\n{ASSIGNMENT_MARKER}\n{payload}\n```"
        )

    assert excinfo.value.code == "duplicate_blocks"


def test_parse_heartbeat_assignment_block_rejects_missing_required_fields():
    payload = {
        "assignment": "Draft a revenue experiment",
        "assigned_to": "sage",
        "assignment_id": "assignment-123",
        "created_at": "2026-05-21T00:00:00+00:00",
        "updated_at": "2026-05-21T00:00:00+00:00",
    }

    with pytest.raises(HeartbeatValidationError) as excinfo:
        parse_heartbeat_assignment_block(
            f"{ASSIGNMENT_MARKER}\n{json.dumps(payload, indent=2, sort_keys=True)}\n```"
        )

    assert excinfo.value.code == "missing_required_fields"
    assert excinfo.value.assignment_id == "assignment-123"
    assert excinfo.value.assigned_to == "sage"


def test_parse_heartbeat_assignment_block_rejects_assigned_to_mismatch():
    assignment = Assignment(
        assignment="Draft a revenue experiment",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    payload = json.dumps(assignment.to_dict(), indent=2, sort_keys=True)

    with pytest.raises(HeartbeatValidationError) as excinfo:
        parse_heartbeat_assignment_block(
            f"{ASSIGNMENT_MARKER}\n{payload}\n```",
            expected_agent_id="garde",
        )

    assert excinfo.value.code == "assigned_to_mismatch"
    assert excinfo.value.assignment_id == assignment.assignment_id
    assert excinfo.value.assigned_to == "sage"


def test_parse_heartbeat_assignment_block_rejects_truncated_fence():
    assignment = Assignment(
        assignment="Draft a revenue experiment",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )

    with pytest.raises(HeartbeatValidationError) as excinfo:
        parse_heartbeat_assignment_block(
            f"{ASSIGNMENT_MARKER}\n{json.dumps(assignment.to_dict(), indent=2, sort_keys=True)}\n"
        )

    assert excinfo.value.code == "truncated_block"


def test_write_heartbeat_assignment_preserves_notes_around_replaced_block(tmp_path):
    agent = Agent(
        agent_id="sage",
        display_name="SAGE",
        workspace_path="workspace-sage",
        role="crew_chief",
    )
    first = Assignment(
        assignment="First",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    second = Assignment(
        assignment="Second",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )

    heartbeat = write_heartbeat_assignment(agent, first, tmp_path)
    first_block = heartbeat.read_text(encoding="utf-8").split("\n\n", 2)[-1]
    heartbeat.write_text(
        "# Heartbeat: SAGE\n\nOperator note above.\n\n"
        + first_block
        + "\nOperator note below.\n",
        encoding="utf-8",
    )

    write_heartbeat_assignment(agent, second, tmp_path)

    text = heartbeat.read_text(encoding="utf-8")
    assert "Operator note above." in text
    assert "Operator note below." in text
    assert '"assignment": "Second"' in text
