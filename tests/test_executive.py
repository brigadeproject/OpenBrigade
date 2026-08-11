"""Release 1.3 Executive concierge foundation."""

from __future__ import annotations

import asyncio
import json
from hashlib import sha256

import pytest

import brigade.executive as executive_module
from brigade.auth import issue_token
from brigade.config import Settings
from brigade.executive import (
    apply_executive_actions,
    resolve_executive_persona,
    run_executive_chat_turn,
)
from brigade.runner import run_managed_agents
from brigade.schemas import Agent, Assignment, Role, User
from brigade.state import JsonStateStore
from brigade.tools import ToolResult
from brigade.workspace import (
    REQUIRED_AGENT_FILES,
    agent_workspace_files,
    ensure_agent_workspace,
)
from tests.helpers import SequencedTestProvider, TestProvider
from tests.test_v0_9 import _asgi_request


def _store(tmp_path):
    store = JsonStateStore(tmp_path / "state.json")
    store.add_user(User("owner", Role.OWNER))
    store.add_agent(
        Agent(
            "exec",
            "Executive",
            "workspace-exec",
            role="executive",
            owner_username="owner",
        )
    )
    store.add_agent(Agent("ada", "ADA", "workspace-ada"))
    return store


def _propose(actions: list[dict], summary: str = "I can do that.") -> str:
    return json.dumps(
        {"status": "propose_actions", "summary": summary, "actions": actions}
    )


def test_executive_requires_owner_username() -> None:
    with pytest.raises(ValueError, match="owner_username"):
        Agent("exec", "Executive", "workspace-exec", role="executive")


def test_executive_workspace_gets_skills_without_changing_required_files(tmp_path):
    store = _store(tmp_path)
    executive = resolve_executive_persona(store, "owner")
    agent = next(item for item in store.agents() if item.agent_id == executive.agent_id)
    ensure_agent_workspace(agent, tmp_path)

    assert "SKILLS.md" not in REQUIRED_AGENT_FILES
    assert (tmp_path / "workspace-exec" / "SKILLS.md").exists()

    worker = next(item for item in store.agents() if item.agent_id == "ada")
    ensure_agent_workspace(worker, tmp_path)
    assert not (tmp_path / "workspace-ada" / "SKILLS.md").exists()


def test_run_managed_agents_skips_executive_when_running_all(tmp_path):
    store = _store(tmp_path)
    store.add_assignment(
        Assignment(
            "Should not run via heartbeat",
            assigned_to="exec",
            created_by="owner",
            source="test",
        )
    )

    assert run_managed_agents(store, TestProvider()) == []


def test_executive_can_stage_and_apply_task_action(tmp_path):
    store = _store(tmp_path)
    persona = resolve_executive_persona(store, "owner")
    thread = store.resolve_active_conversation("owner", persona.persona_id)
    provider = SequencedTestProvider(
        [
            _propose(
                [
                    {
                        "type": "create_task",
                        "agent_id": "ada",
                        "assignment": "Summarize the release notes.",
                        "priority": "high",
                    }
                ]
            )
        ]
    )

    proposed = run_executive_chat_turn(
        store,
        thread=thread,
        persona=persona,
        operator="owner",
        content="Ask ADA to summarize the release notes.",
        provider=provider,
    )
    assert proposed["status"] == "proposed"

    applied = run_executive_chat_turn(
        store,
        thread=thread,
        persona=persona,
        operator="owner",
        content="confirm",
        provider=SequencedTestProvider(["not used"]),
    )
    assert applied["status"] == "applied"
    assignment = store.assignments()[0]
    assert assignment.source == "executive_chat"
    assert assignment.assigned_to == "ada"
    assert assignment.created_by_role == "executive"


def test_legal_executive_answer_requires_a_valid_retrieved_citation(tmp_path, monkeypatch):
    store = _store(tmp_path)
    persona = resolve_executive_persona(store, "owner")
    thread = store.resolve_active_conversation("owner", persona.persona_id)
    source_url = "https://www.law.cornell.edu/uscode/text/18/1030"
    source_id = f"external:{sha256(source_url.encode('utf-8')).hexdigest()[:16]}"
    monkeypatch.setattr(
        executive_module,
        "_web_fetch",
        lambda context, arguments: ToolResult(
            True,
            "secondary interpretation",
            {"source_url": source_url, "final_url": source_url, "title": "LII interpretation"},
        ),
    )
    provider = SequencedTestProvider(
        [
            json.dumps(
                {"status": "tool_call", "tool": "web_fetch", "arguments": {"url": source_url}}
            ),
            f"A secondary interpretation. [[cite:{source_id}]]",
        ]
    )

    result = run_executive_chat_turn(
        store,
        thread=thread,
        persona=persona,
        operator="owner",
        content="Provide a legal citation.",
        provider=provider,
    )

    message = store.messages(result["conversation_id"])[-1]
    assert message.metadata["citation_required"] is True
    assert message.metadata["citations"][0]["source_id"] == source_id
    assert "[^1]: [LII interpretation]" in message.content
    assert "secondary legal" in message.content


def test_executive_actions_can_create_goal_guidance_and_kb_note(tmp_path):
    store = _store(tmp_path)
    assignment = store.add_assignment(
        Assignment("Build a test plan", assigned_to="ada", created_by="owner", source="test")
    )
    persona = resolve_executive_persona(store, "owner")

    result = apply_executive_actions(
        store,
        [
            {
                "type": "create_goal",
                "agent_id": "ada",
                "statement": "Keep release notes current",
                "success_criteria": ["notes updated"],
                "explicitly_not": [],
            },
            {
                "type": "attach_guidance",
                "assignment_id": assignment.assignment_id,
                "message": "Use the changelog as the source of truth.",
            },
            {
                "type": "ingest_text",
                "title": "Executive note",
                "source": "manual",
                "document_type": "note",
                "content": "Remember to brief the owner before release.",
            },
        ],
        persona=persona,
        by="owner",
    )

    assert len(result["applied"]) == 3
    assert store.goals("ada")["ada"][0].statement == "Keep release notes current"
    assert store.find_assignment(assignment.assignment_id).operator_guidance
    assert store.knowledge_documents()[0]["title"] == "Executive note"


def test_agent_workspace_file_whitelist_allows_skills_only_for_executive(tmp_path):
    store = _store(tmp_path)
    executive = next(item for item in store.agents() if item.agent_id == "exec")
    worker = next(item for item in store.agents() if item.agent_id == "ada")

    assert "SKILLS.md" in agent_workspace_files(executive)
    assert "SKILLS.md" not in agent_workspace_files(worker)


def test_executive_thread_routes_send_owner_scoped_turn(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")

    import brigade.web

    provider = SequencedTestProvider(["Executive ready."])
    monkeypatch.setattr(brigade.web, "provider_from_settings", lambda *args, **kwargs: provider)
    store = _store(tmp_path)
    other = User("other", Role.OWNER)
    store.add_user(other)
    settings = Settings(
        config_path=tmp_path / "brigade.config.json",
        data_dir=tmp_path,
        require_auth=True,
        jwt_secret="x" * 40,
        allow_json_store=True,
    )
    app = brigade.web.create_app(settings, store)
    owner_headers = {"Authorization": f"Bearer {issue_token(settings, store.users()[0])}"}
    other_headers = {"Authorization": f"Bearer {issue_token(settings, other)}"}

    listed = asyncio.run(
        _asgi_request(app, "GET", "/api/chat/threads", headers=owner_headers)
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["personas"][0]["persona_id"] == "executive:exec"

    opened = asyncio.run(
        _asgi_request(
            app,
            "POST",
            "/api/chat/threads",
            headers=owner_headers,
            json_payload={"persona": "executive:exec"},
        )
    )
    assert opened.status_code == 200, opened.text
    thread = opened.json()
    assert thread["persona"] == "executive:exec"
    assert thread["operator_username"] == "owner"

    sent = asyncio.run(
        _asgi_request(
            app,
            "POST",
            f"/api/chat/threads/{thread['thread_id']}/messages",
            headers=owner_headers,
            json_payload={"content": "Check in.", "provider": "test", "model": "test-model"},
        )
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == "complete"

    messages = asyncio.run(
        _asgi_request(
            app,
            "GET",
            f"/api/chat/threads/{thread['thread_id']}/messages",
            headers=owner_headers,
        )
    )
    assert messages.status_code == 200
    assert [item["sender"] for item in messages.json()["messages"]] == ["owner", "exec"]
    assert messages.json()["messages"][1]["content"] == "Executive ready."

    denied = asyncio.run(
        _asgi_request(
            app,
            "GET",
            f"/api/chat/threads/{thread['thread_id']}/messages",
            headers=other_headers,
        )
    )
    assert denied.status_code == 403
