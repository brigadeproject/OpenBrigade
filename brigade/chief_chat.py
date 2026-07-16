"""Natural-language chat with Crew Chiefs (release 1.1).

An operator converses with one persona per thread: a team's Crew Chief
(scoped to that chief's managed agents) or the fleet-wide "front desk"
(the Orchestrator's view). Threads are durable ``Conversation`` records;
their messages live in the ordinary chat log under ``thread:<id>``.
"""

from __future__ import annotations

from dataclasses import dataclass

from brigade.prompt_floors import _managed_agent_ids
from brigade.schemas import Team
from brigade.store import StateStore

FRONT_DESK_PERSONA = "front_desk"


class UnknownPersonaError(ValueError):
    """The requested persona does not match front desk or any crew chief."""


@dataclass(frozen=True)
class Persona:
    """A resolved chat persona: front desk, or one team's crew chief."""

    persona_id: str  # "front_desk" or "chief:<agent_id>"
    kind: str  # "front_desk" | "chief"
    display_name: str
    chief_agent_id: str | None = None
    team_id: str | None = None
    managed_agent_ids: frozenset[str] = frozenset()

    @property
    def is_front_desk(self) -> bool:
        return self.kind == FRONT_DESK_PERSONA

    def to_dict(self) -> dict[str, object]:
        return {
            "persona_id": self.persona_id,
            "kind": self.kind,
            "display_name": self.display_name,
            "chief_agent_id": self.chief_agent_id,
            "team_id": self.team_id,
            "managed_agent_ids": sorted(self.managed_agent_ids),
        }


def _front_desk_persona() -> Persona:
    return Persona(
        persona_id=FRONT_DESK_PERSONA,
        kind=FRONT_DESK_PERSONA,
        display_name="Front desk",
    )


def _chief_persona(store: StateStore, teams: list[Team], team: Team) -> Persona:
    chief_id = str(team.crew_chief_id)
    agent = next((item for item in store.agents() if item.agent_id == chief_id), None)
    display = agent.display_name if agent else chief_id
    return Persona(
        persona_id=f"chief:{chief_id}",
        kind="chief",
        display_name=f"{display} ({team.display_name})",
        chief_agent_id=chief_id,
        team_id=team.team_id,
        managed_agent_ids=frozenset(_managed_agent_ids(teams, chief_id)),
    )


def available_personas(store: StateStore) -> list[Persona]:
    """Front desk plus one persona per team that has a crew chief."""
    teams = store.teams()
    personas = [_front_desk_persona()]
    seen_chiefs: set[str] = set()
    for team in teams:
        if not team.crew_chief_id or team.crew_chief_id in seen_chiefs:
            continue
        seen_chiefs.add(team.crew_chief_id)
        personas.append(_chief_persona(store, teams, team))
    return personas


def resolve_persona(
    store: StateStore,
    requested: str | None,
    *,
    default: str = "auto",
) -> Persona:
    """Resolve a persona request to a concrete Persona.

    Accepts ``front_desk``/``frontdesk``, ``chief:<agent_id>``, a bare chief
    agent id, a team id, or a display-name fragment (case-insensitive; used
    by the connector ``/chief`` command). ``None``/``auto`` fall back to the
    configured default: a single-chief fleet talks to that chief, anything
    else to the front desk.
    """
    personas = available_personas(store)
    chiefs = [item for item in personas if item.kind == "chief"]

    normalized = (requested or "").strip().lower()
    if not normalized or normalized == "auto":
        if default != "auto":
            return resolve_persona(store, default, default="auto")
        if len(chiefs) == 1:
            return chiefs[0]
        return personas[0]
    if normalized in {FRONT_DESK_PERSONA, "frontdesk", "front-desk", "orchestrator"}:
        return personas[0]

    target = normalized.removeprefix("chief:")
    for persona in chiefs:
        if target in {
            str(persona.chief_agent_id).lower(),
            str(persona.team_id).lower(),
        }:
            return persona
    # Display-name fragment as a last resort, only when unambiguous.
    fragment_matches = [
        persona for persona in chiefs if target and target in persona.display_name.lower()
    ]
    if len(fragment_matches) == 1:
        return fragment_matches[0]
    raise UnknownPersonaError(
        f"unknown persona: {requested!r}; expected front_desk, a chief agent id, or a team id"
    )
