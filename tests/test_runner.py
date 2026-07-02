from __future__ import annotations

import json

from brigade.providers import ModelResponse
from brigade.runner import run_agent_once
from brigade.schemas import Agent, Assignment
from brigade.state import JsonStateStore
from brigade.workspace import write_heartbeat_assignment
from tests.helpers import TestProvider


class _SequencedProvider:
    """Returns each prompted response in order, recording every prompt seen."""

    __test__ = False
    route_type = "test"

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> ModelResponse:
        self.prompts.append(prompt)
        text = self._texts.pop(0)
        return ModelResponse(
            text=text,
            input_tokens=len(prompt.split()),
            output_tokens=len(text.split()),
            provider="test",
            model="test-model",
            route_type=self.route_type,
        )


def test_malformed_response_retry_feeds_back_the_parse_error(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    assignment = Assignment(
        assignment="Draft a plan",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    assignment.transition_to(status=assignment.status.ASSIGNED)
    store.add_agent(agent)
    store.add_assignment(assignment)
    write_heartbeat_assignment(agent, assignment, tmp_path)

    good_response = json.dumps(
        {"status": "complete", "summary": "done after retry", "blockers": []}
    )
    provider = _SequencedProvider(['{"status": "complete", "summary":', good_response])

    result = run_agent_once("sage", store, provider)

    assert result.status == "complete"
    assert len(provider.prompts) == 2
    retry_prompt = provider.prompts[1]
    assert "could not be parsed" in retry_prompt
    assert '{"status": "complete", "summary":' in retry_prompt


def test_run_agent_once_completes_and_archives_assignment(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    agent = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    assignment = Assignment(
        assignment="Draft a plan",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    assignment.transition_to(status=assignment.status.ASSIGNED)
    store.add_agent(agent)
    store.add_assignment(assignment)
    write_heartbeat_assignment(agent, assignment, tmp_path)

    result = run_agent_once("sage", store, TestProvider())

    assert result.status == "complete"
    assert store.assignments() == []
    assert store.assignment_history()[0]["assignment_id"] == assignment.assignment_id
    heartbeat = tmp_path / "workspace-sage" / "HEARTBEAT.md"
    assert '"status": "complete"' in heartbeat.read_text(encoding="utf-8")


def _make_agent_with_assignment(tmp_path, store):
    agent = Agent(agent_id="sage", display_name="SAGE", workspace_path="workspace-sage")
    assignment = Assignment(
        assignment="Draft a report",
        assigned_to="sage",
        created_by="human",
        source="direct_command",
    )
    assignment.transition_to(status=assignment.status.ASSIGNED)
    store.add_agent(agent)
    store.add_assignment(assignment)
    write_heartbeat_assignment(agent, assignment, tmp_path)
    return agent, assignment


def test_completion_claiming_missing_file_is_rejected_then_accepted(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    _make_agent_with_assignment(tmp_path, store)

    fabricated = json.dumps(
        {"status": "complete", "summary": "Saved findings to report.md", "blockers": []}
    )
    corrected = json.dumps(
        {
            "status": "complete",
            "summary": "Summarized findings inline; no files were written",
            "blockers": [],
        }
    )
    provider = _SequencedProvider([fabricated, corrected])

    result = run_agent_once("sage", store, provider)

    assert result.status == "complete"
    assert "no files were written" in result.summary
    assert len(provider.prompts) == 2
    assert "do not exist in your workspace" in provider.prompts[1]
    assert "report.md" in provider.prompts[1]


def test_completion_claiming_existing_file_passes(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    _make_agent_with_assignment(tmp_path, store)
    (tmp_path / "workspace-sage" / "report.md").write_text("data", encoding="utf-8")

    response = json.dumps(
        {"status": "complete", "summary": "Saved findings to report.md", "blockers": []}
    )
    provider = _SequencedProvider([response])

    result = run_agent_once("sage", store, provider)

    assert result.status == "complete"
    assert len(provider.prompts) == 1


def test_persistent_fabricated_completion_is_downgraded_to_working(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    _make_agent_with_assignment(tmp_path, store)

    fabricated = json.dumps(
        {"status": "complete", "summary": "Wrote Bylaws.md in the workspace", "blockers": []}
    )
    provider = _SequencedProvider([fabricated, fabricated])

    result = run_agent_once("sage", store, provider)

    assert result.status != "complete"
    assert "completion rejected" in result.summary
    assert "Bylaws.md" in result.summary
    # The assignment stays active rather than being archived as done.
    assert store.assignments() != []


def test_claimed_file_paths_extraction():
    from brigade.runner import _claimed_file_paths

    summary = (
        "Wrote `notes/plan.md` and /data/workspace-sage/Bylaws.md; see "
        "https://sos.ri.gov/forms/guide.pdf and www.example.com/x.md for sources. "
        "Also updated report.md."
    )
    claims = _claimed_file_paths(summary)
    assert "notes/plan.md" in claims
    assert "/data/workspace-sage/Bylaws.md" in claims
    assert "report.md" in claims
    assert all("sos.ri.gov" not in claim for claim in claims)
    assert all("example.com" not in claim for claim in claims)


def test_non_complete_status_with_missing_file_claims_is_annotated(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    _make_agent_with_assignment(tmp_path, store)

    response = json.dumps(
        {
            "status": "awaiting_human",
            "summary": "Prepared filing package in Submission_Package.md; human must file it",
            "blockers": ["manual SOS filing required"],
        }
    )
    provider = _SequencedProvider([response])

    result = run_agent_once("sage", store, provider)

    assert result.status != "complete"
    assert "files mentioned but not found in workspace" in result.summary
    assert "Submission_Package.md" in result.summary


def test_native_tool_specs_conversion():
    from brigade.runner import _native_tool_specs
    from brigade.tools import default_tool_registry

    specs = _native_tool_specs(default_tool_registry())
    by_name = {spec["function"]["name"]: spec for spec in specs}
    assert "write_file" in by_name
    write_file = by_name["write_file"]["function"]
    assert write_file["parameters"]["type"] == "object"
    assert "path" in write_file["parameters"]["properties"]
    assert "path" in write_file["parameters"]["required"]
    assert "content" in write_file["parameters"]["required"]
    assert "append" not in write_file["parameters"]["required"]


def test_tool_observations_are_truncated_in_prompt(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    _make_agent_with_assignment(tmp_path, store)
    (tmp_path / "workspace-sage" / "big.md").write_text("x" * 11_000, encoding="utf-8")

    tool_call = json.dumps(
        {
            "status": "tool_call",
            "tool": "read_file",
            "arguments": {"path": "big.md"},
            "summary": "read the big file",
        }
    )
    done = json.dumps({"status": "complete", "summary": "reviewed the data", "blockers": []})
    provider = _SequencedProvider([tool_call, done])

    result = run_agent_once("sage", store, provider)

    assert result.status == "complete"
    followup_prompt = provider.prompts[1]
    assert "<truncated>" in followup_prompt
    assert "x" * 5000 not in followup_prompt
