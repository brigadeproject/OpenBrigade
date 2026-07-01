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
