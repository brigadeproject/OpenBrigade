"""Derived agent capability profiles.

Everything here is computed on read from state that already exists (archived
assignment history and workspace-built tools). The operator-curated
``Agent.specialties`` field stays the only stored profile input, so curation
is never clobbered and there is no refresh job to schedule.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from brigade.schemas import Agent, AssignmentStatus
from brigade.store import StateStore
from brigade.tools import workspace_tool_manifest

MAX_DERIVED_SPECIALTIES = 8
MAX_RECENT_COMPLETIONS = 3
MAX_COMPLETION_SUMMARY_CHARS = 160
# Tokens must recur across at least this many completed assignments to count
# as a demonstrated specialty rather than one-off vocabulary.
MIN_TOKEN_OCCURRENCES = 2

# Generic assignment/orchestration vocabulary that recurs in almost every
# assignment text regardless of the work's subject matter.
STOPWORDS = frozenset(
    """
    the and for with that this from into your you are was were has have had not
    all any can will must should each every when then than them they its
    it's about after before between during under over more most some such only
    also just like make made use used using work working task tasks assignment
    assignments subtask subtasks team teams member members agent agents goal
    goals mission review create created done complete completed finish finished
    next step steps plan plans planning decompose delegate route routing new
    file files write read report toward advance concrete define which whose
    who what where why how their our own out one two three first second
    """.split()
)


def _tokens(text: str) -> list[str]:
    cleaned = [
        token.strip(".,;:!?()[]'\"`")
        for token in text.lower().split()
    ]
    return [
        token
        for token in cleaned
        if len(token) > 2 and token not in STOPWORDS and not token.isdigit()
    ]


def derive_agent_profile(
    store: StateStore,
    agent: Agent,
    *,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """What this agent has demonstrably done: built tools, recurring themes in
    completed work, and its latest completions.

    Pass ``history`` (``store.assignment_history()``) when profiling several
    agents so the archive is fetched once.
    """
    completions = [
        item
        for item in (store.assignment_history() if history is None else history)
        if item.get("final_status") == AssignmentStatus.COMPLETE.value
        and (item.get("record") or {}).get("assigned_to") == agent.agent_id
    ]

    token_counts: Counter[str] = Counter()
    for item in completions:
        record = item.get("record") or {}
        seen = set(_tokens(str(record.get("assignment") or "")))
        seen |= set(_tokens(str(item.get("executive_summary") or "")))
        token_counts.update(seen)
    curated = {token for specialty in agent.specialties for token in _tokens(specialty)}
    derived_specialties = [
        token
        for token, count in token_counts.most_common()
        if count >= MIN_TOKEN_OCCURRENCES and token not in curated
    ][:MAX_DERIVED_SPECIALTIES]

    recent_completions = [
        {
            "assignment_id": item.get("assignment_id"),
            "summary": str(
                item.get("executive_summary")
                or (item.get("record") or {}).get("assignment")
                or ""
            )[:MAX_COMPLETION_SUMMARY_CHARS],
        }
        # History is archived_at-ascending; the newest completions are last.
        for item in completions[-MAX_RECENT_COMPLETIONS:][::-1]
    ]

    built_tools = [
        {"name": tool["name"], "description": tool["description"]}
        for tool in workspace_tool_manifest(store.data_dir / agent.workspace_path)
    ]

    return {
        "built_tools": built_tools,
        "derived_specialties": derived_specialties,
        "recent_completions": recent_completions,
    }


def derived_specialty_tokens(
    store: StateStore,
    agent: Agent,
    *,
    history: list[dict[str, Any]] | None = None,
) -> set[str]:
    """Flat token set for deterministic matchers (ladder, intake): derived
    themes plus built-tool names/descriptions."""
    profile = derive_agent_profile(store, agent, history=history)
    tokens: set[str] = set(profile["derived_specialties"])
    for tool in profile["built_tools"]:
        tokens.update(_tokens(f"{tool['name']} {tool['description']}"))
    return tokens
