# v0.6 Organization and Delegation

Planned scope for the phase after v0.5 onboarding and team CRUD. Focus: make team hierarchy operational inside the orchestrator instead of only visible in the UI.

## Implementation Status

The first v0.6 prototype pass is implemented:

- Team records now carry `delegation_policy` and optional `escalation_team_id`.
- Parent-team Crew Chiefs can delegate into child teams unless the target team is `orchestrator_only`.
- `task create --source agent_delegate` validates that agent-created work stays inside allowed authority boundaries.
- `team route-work` routes team work to a Crew Chief or individual member and records the routing decision.
- `team escalate` creates cross-team assignments, chat records, and reasoning records.
- `team status` and the dashboard teams view show team-scoped goals, active assignments, and blockers.
- `org graph --persist` emits and stores organization graph snapshots as provenance records.

## Add hierarchical delegation rules for teams and sub-teams

- Define which agents can assign work directly, which must escalate through a Crew Chief, and which can only receive orchestrator-issued work.
- Model delegation as explicit policy, not inferred from display hierarchy alone.

## Add team-aware orchestrator policy for routing work to Crew Chiefs or individual agents

- Let the orchestrator decide whether to assign work to an individual contributor, a Crew Chief, or a whole team queue based on goal scope, urgency, and dependencies.
- Keep routing decisions explainable in orchestrator reasoning.

## Add authority validation so agents can only direct allowed team members

- Enforce command boundaries at task creation, reassignment, and chat escalation points.
- Reject unauthorized delegation with clear operator-facing reasons.

## Add cross-team coordination rules and escalation paths

- Define how work moves between teams, when a Crew Chief can request support from another team, and when owner approval is required.
- Record escalation reason, source team, destination team, and final disposition.

## Add team-scoped goal and status views

- Show goals, active tasks, blockers, and recent progress at the team level, not only per-agent.
- Make these views available in both the dashboard and non-interactive CLI output.

## Add organization graph storage for teams, reporting lines, and command relationships

- Persist teams, memberships, reporting lines, and delegation edges in a first-class structure.
- Use that structure as the source of truth for UI, routing, and authorization decisions.
