from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from brigade import __version__
from brigade.auth import (
    DEFAULT_JWT_SECRET,
    AuthResult,
    build_user_identity_context,
    issue_token,
    verify_token,
)
from brigade.config import Settings, load_settings
from brigade.connectors import (
    approve_external_identity,
    handle_google_chat_event,
    handle_telegram_update,
    parse_allowlist,
    reject_external_identity,
)
from brigade.datastores import Neo4jProvenanceStore, QdrantEpisodeStore
from brigade.db import (
    MigrationApplyError,
    apply_migrations,
    combined_schema_sql,
    load_migrations,
    migration_status,
)
from brigade.export import export_training_data
from brigade.finance import build_model_routing_decision
from brigade.health import HealthCheck, check_configured_datastores
from brigade.knowledge import ingest_local_document
from brigade.logging import configure_json_logging
from brigade.memory import (
    append_daily_memory,
    archive_stale_daily_memories,
    curate_workspace_memory,
)
from brigade.orchestrator import (
    FullCycleResult,
    OrchestrationConfig,
    ProactiveContinuationConfig,
    orchestration_event,
    run_full_cycle,
)
from brigade.providers import (
    LiteLLMProvider,
    ModelProvider,
    OllamaProvider,
    ProviderAuthError,
    available_model_options,
    probe_model_inventory,
    provider_from_settings,
)
from brigade.rbac import can
from brigade.runner import (
    _acquire_local_inference_lock,
    _release_local_inference_lock,
    is_local_inference_backpressure,
    run_agent_once,
    run_managed_agents,
)
from brigade.schemas import (
    PROPOSAL_KINDS,
    PROPOSAL_STATUSES,
    TERMINAL_STATUSES,
    Agent,
    Assignment,
    AssignmentKind,
    AssignmentStatus,
    ChatMessage,
    Goal,
    GoalEngagementMode,
    Mission,
    Priority,
    Role,
    Team,
    User,
    WorkMode,
)
from brigade.secrets import (
    MODEL_AUTH_PROVIDERS,
    delete_oauth_credential,
    oauth_credential_status,
    write_oauth_credential,
)
from brigade.services import (
    AssignmentActionError,
    build_chat_payload,
    build_settings_payload,
    cancel_assignment,
    cancel_assignments_where,
    decide_proposal,
    delegate_from_crew_chief,
    reissue_assignment,
    send_user_chat,
    set_config_value,
)
from brigade.store import RedisRuntimeClient, StateStore, open_state_store
from brigade.time import utc_now_iso
from brigade.tui import (
    VIEWS,
    build_dashboard_payload,
    render_chat_view,
    render_dashboard_view,
    render_settings_view,
    run_chat_tui,
    run_dashboard_tui,
    run_settings_tui,
)
from brigade.workspace import ensure_agent_workspace, validate_agent_workspace


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brigade")
    parser.add_argument("--version", action="version", version=f"brigade {__version__}")
    parser.add_argument("--as-user", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument(
        "--allow-host-state",
        action="store_true",
        help="Bypass the host-state guard and use the local .brigade directory directly.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    config = subcommands.add_parser("config")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("show")
    config_inspect = config_sub.add_parser("inspect")
    config_inspect.add_argument("--json", action="store_true")
    config_set = config_sub.add_parser("set")
    config_set.add_argument("--key", required=True)
    config_set.add_argument("--value", required=True)
    config_set.add_argument(
        "--base-hash",
        default=None,
        help="Optional config_hash from config inspect/settings output; rejects stale writes.",
    )

    settings_command = subcommands.add_parser(
        "settings",
        help="Inspect settings through a TUI or plain output.",
    )
    settings_sub = settings_command.add_subparsers(dest="settings_command", required=True)
    settings_tui = settings_sub.add_parser("tui")
    settings_tui.add_argument("--plain", action="store_true")
    settings_tui.add_argument(
        "--refresh-seconds",
        type=float,
        default=2.0,
        help="Refresh cadence for the interactive settings view. Default: 2.0 seconds.",
    )

    auth = subcommands.add_parser("auth")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    auth_issue = auth_sub.add_parser("issue")
    auth_issue.add_argument("--username", required=True)
    auth_issue.add_argument(
        "--role",
        choices=[item.value for item in Role],
        default=None,
        help="Create the user with this role if it does not already exist.",
    )
    auth_issue.add_argument(
        "--ttl-seconds",
        type=int,
        default=3600,
        help="Token lifetime in seconds. Default: 3600.",
    )
    auth_verify = auth_sub.add_parser("verify")
    auth_verify.add_argument("--token-value", required=True)

    db = subcommands.add_parser("db")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    db_sub.add_parser("schema")
    db_sub.add_parser("migrations")
    db_sub.add_parser("status")
    db_sub.add_parser("migrate")

    datastore = subcommands.add_parser(
        "datastore",
        help="Inspect external datastore health and samples.",
    )
    datastore_sub = datastore.add_subparsers(dest="datastore_command", required=True)
    datastore_inspect = datastore_sub.add_parser(
        "inspect",
        help="Inspect Redis, Qdrant, or Neo4j records.",
    )
    datastore_inspect.add_argument("--backend", choices=["redis", "qdrant", "neo4j"], required=True)
    datastore_inspect.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Sample limit. Default: 10.",
    )

    init = subcommands.add_parser("init")
    init_sub = init.add_subparsers(dest="init_command", required=True)
    init_mvp = init_sub.add_parser("mvp")
    init_mvp.add_argument(
        "--mission",
        default="Make enough money to offset OpenBrigade operating cost.",
        help=(
            "Initial mission statement. Default: "
            "'Make enough money to offset OpenBrigade operating cost.'"
        ),
    )
    init_mvp.add_argument(
        "--force",
        action="store_true",
        help="Re-seed defaults into an existing prototype state.",
    )

    user = subcommands.add_parser("user")
    user_sub = user.add_subparsers(dest="user_command", required=True)
    user_add = user_sub.add_parser("add")
    user_add.add_argument("--username", required=True)
    user_add.add_argument(
        "--role",
        choices=[item.value for item in Role],
        default=Role.OBSERVER.value,
        help="User role to assign. Default: observer.",
    )
    user_sub.add_parser("list")

    mission = subcommands.add_parser("mission")
    mission_sub = mission.add_subparsers(dest="mission_command", required=True)
    mission_set = mission_sub.add_parser("set")
    mission_set.add_argument("--statement", required=True)
    mission_set.add_argument("--success", action="append", default=[])
    mission_set.add_argument("--not", dest="explicitly_not", action="append", default=[])
    mission_sub.add_parser("show")

    agent = subcommands.add_parser("agent")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_add = agent_sub.add_parser("add")
    agent_add.add_argument("--id", required=True)
    agent_add.add_argument("--name", required=True)
    agent_add.add_argument("--workspace", required=True)
    agent_add.add_argument(
        "--role",
        default="line_worker",
        help="Agent role label. Default: line_worker.",
    )
    agent_onboard = agent_sub.add_parser(
        "onboard",
        help="Register an agent and create its workspace manifest.",
        description=(
            "Create or update an agent, generate required workspace files, "
            "and optionally attach the agent to a team."
        ),
    )
    agent_onboard.add_argument("--id", required=True)
    agent_onboard.add_argument("--name", required=True)
    agent_onboard.add_argument(
        "--workspace",
        default=None,
        help="Workspace path. Default: workspace-{id}.",
    )
    agent_onboard.add_argument(
        "--role",
        default="line_worker",
        help="Agent role label. Default: line_worker.",
    )
    agent_onboard.add_argument("--team", default=None, help="Optional team id to join.")
    agent_onboard.add_argument(
        "--create-team",
        action="store_true",
        help="Create --team if it does not exist.",
    )
    agent_onboard.add_argument(
        "--crew-chief",
        action="store_true",
        help="Mark this agent as the Crew Chief for --team.",
    )
    agent_onboard.add_argument(
        "--provider",
        default=None,
        help="Default model provider for this agent. Default: resolved settings provider.",
    )
    agent_onboard.add_argument(
        "--model",
        default=None,
        help="Default model name for this agent. Default: resolved settings model.",
    )
    agent_onboard.add_argument(
        "--specialty",
        dest="specialties",
        action="append",
        default=[],
        help="Specialty tag for routing hints. Repeat for multiple. Empty means generalist.",
    )
    agent_validate = agent_sub.add_parser(
        "validate",
        help="Validate one agent workspace manifest.",
    )
    agent_validate.add_argument("--id", required=True)
    agent_validate.add_argument(
        "--repair",
        action="store_true",
        help="Create any missing default workspace files before validating.",
    )
    agent_run = agent_sub.add_parser("run")
    agent_run.add_argument("--id", required=True)
    _add_provider_args(agent_run)
    agent_run_all = agent_sub.add_parser("run-all")
    _add_provider_args(agent_run_all)
    agent_model = agent_sub.add_parser(
        "model",
        help="Update one agent's persisted model provider and model.",
    )
    agent_model.add_argument("--id", required=True)
    agent_model.add_argument(
        "--provider",
        required=True,
        choices=["ollama", "litellm", "openai", "openai-codex", "anthropic", "gemini"],
    )
    agent_model.add_argument("--model", required=True)
    agent_update = agent_sub.add_parser(
        "update",
        help="Update one agent's role and/or specialties.",
    )
    agent_update.add_argument("--id", required=True)
    agent_update.add_argument(
        "--role",
        default=None,
        choices=["line_worker", "crew_chief"],
        help="New role label.",
    )
    agent_update.add_argument(
        "--specialty",
        dest="specialties",
        action="append",
        default=None,
        help=(
            "Specialty tag for routing hints. Repeat for multiple; the full "
            "list replaces the agent's current specialties. Pass a single "
            "empty value ('') to clear them."
        ),
    )
    agent_sub.add_parser("list")

    team = subcommands.add_parser(
        "team",
        help="Create, assign, and inspect teams.",
        description="Manage team membership and Crew Chief assignment for agents.",
    )
    team_sub = team.add_subparsers(dest="team_command", required=True, metavar="command")
    team_create = team_sub.add_parser("create", help="Create or update a team.")
    team_create.add_argument("--id", required=True, help="Stable team id.")
    team_create.add_argument("--name", required=True, help="Display name.")
    team_create.add_argument("--description", default=None, help="Optional team description.")
    team_create.add_argument("--parent", default=None, help="Optional parent team id.")
    team_create.add_argument(
        "--delegation-policy",
        choices=["open", "chief_only", "orchestrator_only"],
        default="chief_only",
        help="Team delegation policy. Default: chief_only.",
    )
    team_create.add_argument(
        "--escalation-team",
        default=None,
        help="Optional default team id for cross-team escalation.",
    )
    team_sub.add_parser("list", help="List teams.")
    team_show = team_sub.add_parser("show", help="Show one team or the full hierarchy.")
    team_show.add_argument("--id", default=None, help="Optional team id.")
    team_status = team_sub.add_parser("status", help="Show team-scoped goals and work.")
    team_status.add_argument("--team", required=True, help="Team id.")
    team_assign = team_sub.add_parser("assign", help="Assign an agent to a team.")
    team_assign.add_argument("--team", required=True)
    team_assign.add_argument("--agent", required=True)
    team_assign.add_argument("--crew-chief", action="store_true")
    team_chief = team_sub.add_parser("chief", help="Set a team's Crew Chief.")
    team_chief.add_argument("--team", required=True)
    team_chief.add_argument("--agent", required=True)
    team_policy = team_sub.add_parser("policy", help="Update team delegation policy.")
    team_policy.add_argument("--team", required=True, help="Team id.")
    team_policy.add_argument(
        "--delegation-policy",
        choices=["open", "chief_only", "orchestrator_only"],
        required=True,
        help="Delegation policy to apply.",
    )
    team_policy.add_argument(
        "--escalation-team",
        default=None,
        help="Optional default escalation team id.",
    )
    team_delegate = team_sub.add_parser(
        "delegate",
        help="Let a team's Crew Chief create work for a team member.",
    )
    team_delegate.add_argument("--team", required=True, help="Team id.")
    team_delegate.add_argument("--chief", required=True, help="Crew Chief agent id.")
    team_delegate.add_argument("--agent", required=True, help="Team member receiving the work.")
    team_delegate.add_argument("--assignment", required=True, help="Assignment text.")
    team_delegate.add_argument("--goal-statement", default=None, help="Optional linked goal.")
    team_delegate.add_argument(
        "--rationale",
        default=None,
        help="Delegation rationale. Default: Crew Chief delegated team work.",
    )
    team_delegate.add_argument(
        "--priority",
        choices=[item.value for item in Priority],
        default=Priority.NORMAL.value,
        help="Task priority. Default: normal.",
    )
    team_route = team_sub.add_parser(
        "route-work",
        help="Route work to a team Chief or individual member using team policy.",
    )
    team_route.add_argument("--team", required=True, help="Team id.")
    team_route.add_argument("--assignment", required=True, help="Assignment text.")
    team_route.add_argument(
        "--scope",
        choices=["team", "individual"],
        default="team",
        help="Routing scope. Default: team.",
    )
    team_route.add_argument(
        "--urgency",
        choices=["low", "normal", "high", "urgent"],
        default="normal",
        help="Urgency used for routing. Default: normal.",
    )
    team_route.add_argument("--goal-statement", default=None, help="Optional linked goal.")
    team_escalate = team_sub.add_parser(
        "escalate",
        help="Escalate a work request from one team to another.",
    )
    team_escalate.add_argument("--from-team", required=True, help="Source team id.")
    team_escalate.add_argument("--to-team", required=True, help="Destination team id.")
    team_escalate.add_argument("--chief", required=True, help="Source Crew Chief agent id.")
    team_escalate.add_argument("--assignment", required=True, help="Escalated work request.")
    team_escalate.add_argument("--reason", required=True, help="Escalation reason.")

    goal = subcommands.add_parser("goal")
    goal_sub = goal.add_subparsers(dest="goal_command", required=True)
    goal_add = goal_sub.add_parser("add")
    goal_add.add_argument("--agent", required=True)
    goal_add.add_argument("--statement", required=True)
    goal_add.add_argument("--success", action="append", default=[])
    goal_add.add_argument("--not", dest="explicitly_not", action="append", required=True)
    goal_add.add_argument("--set-by", default="human", help="Who set the goal. Default: human.")
    goal_add.add_argument("--human-confirmed", action="store_true")
    goal_add.add_argument(
        "--engagement-mode",
        choices=[item.value for item in GoalEngagementMode],
        default=GoalEngagementMode.DIRECTIVE.value,
        help="directive teams work continuously; on_call teams activate on routed work.",
    )
    goal_list = goal_sub.add_parser("list")
    goal_list.add_argument("--agent", default=None)

    export = subcommands.add_parser(
        "export",
        help="Export accumulated history as training data.",
    )
    export_sub = export.add_subparsers(dest="export_command", required=True)
    export_training = export_sub.add_parser(
        "training-data",
        help="Write cycles, assignments, transcripts, usage, episodes, and "
        "proposals as JSONL plus a manifest.",
    )
    export_training.add_argument("--out", required=True, help="Output directory.")
    export_training.add_argument(
        "--since",
        default=None,
        help="Optional UTC ISO timestamp; only newer records are exported.",
    )

    proposal = subcommands.add_parser(
        "proposal",
        help="List and decide pending proposals (efficiency, tool requests, rest insights).",
    )
    proposal_sub = proposal.add_subparsers(dest="proposal_command", required=True)
    proposal_list = proposal_sub.add_parser("list", help="List proposals.")
    proposal_list.add_argument(
        "--kind",
        choices=sorted(PROPOSAL_KINDS),
        default=None,
        help="Optional proposal kind filter.",
    )
    proposal_list.add_argument(
        "--status",
        choices=sorted(PROPOSAL_STATUSES),
        default=None,
        help="Optional proposal status filter.",
    )
    proposal_approve = proposal_sub.add_parser("approve", help="Approve one proposal.")
    proposal_approve.add_argument("proposal_id", help="Proposal id to approve.")
    proposal_reject = proposal_sub.add_parser("reject", help="Reject one proposal.")
    proposal_reject.add_argument("proposal_id", help="Proposal id to reject.")
    proposal_reject.add_argument("--reason", default=None, help="Optional rejection reason.")

    task = subcommands.add_parser(
        "task",
        help="Create, inspect, and list assignments.",
        description=(
            "Create work assignments for agents, inspect one assignment in detail, "
            "or list active assignments."
        ),
    )
    task_sub = task.add_subparsers(dest="task_command", required=True, metavar="command")
    create = task_sub.add_parser("create", help="Create one assignment.")
    create.add_argument("--agent", required=True, help="Agent id that should receive the work.")
    create.add_argument("--assignment", required=True, help="Assignment text to give the agent.")
    create.add_argument(
        "--created-by",
        default="human",
        help="Free-form creator label. Default: human.",
    )
    create.add_argument(
        "--source",
        default="direct_command",
        help="Source label for the task. Default: direct_command.",
    )
    create.add_argument(
        "--priority",
        choices=[item.value for item in Priority],
        default=Priority.NORMAL.value,
        help="Task priority. Default: normal.",
    )
    create.add_argument(
        "--work-mode",
        choices=[item.value for item in WorkMode],
        default=WorkMode.HEARTBEAT.value,
        help="Execution mode for the agent. Default: heartbeat.",
    )
    create.add_argument(
        "--kind",
        choices=[item.value for item in AssignmentKind],
        default=AssignmentKind.MISSION.value,
        help="Assignment kind. Default: mission.",
    )
    create.add_argument(
        "--estimated-cycles",
        type=int,
        default=1,
        help="Expected number of agent cycles before completion. Default: 1.",
    )
    create.add_argument(
        "--depends-on",
        action="append",
        default=[],
        help="Assignment id dependency. Repeat for multiple dependencies.",
    )
    create.add_argument("--goal-statement", default=None, help="Optional higher-level goal.")
    create.add_argument(
        "--rationale",
        default=None,
        help="Why this task belongs with the chosen agent.",
    )
    create.add_argument(
        "--idempotency-key",
        default=None,
        help="Optional client key for duplicate-submission control.",
    )
    task_list = task_sub.add_parser("list", help="List assignments.")
    task_list.add_argument("--agent", default=None, help="Optional agent id filter.")
    task_list.add_argument(
        "--status",
        choices=[item.value for item in AssignmentStatus],
        default=None,
        help="Optional assignment status filter.",
    )
    task_inspect = task_sub.add_parser("inspect", help="Inspect one assignment.")
    task_inspect.add_argument("--id", required=True, help="Assignment id to inspect.")
    task_sub.add_parser("prompt", help="Create one assignment interactively.")
    task_cancel = task_sub.add_parser("cancel", help="Cancel one assignment.")
    task_cancel.add_argument("--id", required=True, help="Assignment id to cancel.")
    task_cancel.add_argument(
        "--reason",
        default="cancelled by operator",
        help="Cancellation reason recorded in history.",
    )
    task_cancel.add_argument(
        "--force",
        action="store_true",
        help="Cancel even with active children/dependents (they are orphaned/released).",
    )
    task_reissue = task_sub.add_parser(
        "reissue",
        help="Reset a blocked assignment's failure state and re-dispatch it.",
    )
    task_reissue.add_argument("--id", required=True, help="Blocked assignment id to reissue.")
    task_cancel_all = task_sub.add_parser(
        "cancel-all",
        help="Bulk-cancel active assignments matching a filter.",
    )
    task_cancel_all.add_argument(
        "--status",
        choices=[item.value for item in AssignmentStatus],
        default=None,
        help="Only cancel assignments with this status.",
    )
    task_cancel_all.add_argument(
        "--blocker",
        default=None,
        help="Only cancel assignments whose blockers contain this substring.",
    )
    task_cancel_all.add_argument(
        "--kind",
        choices=[item.value for item in AssignmentKind],
        default=None,
        help="Only cancel assignments of this kind.",
    )
    task_cancel_all.add_argument(
        "--reason",
        default="bulk cancel by operator",
        help="Cancellation reason recorded in history.",
    )
    task_cancel_all.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be cancelled without changing anything.",
    )

    status = subcommands.add_parser("status")
    status.add_argument("--json", action="store_true")

    dashboard = subcommands.add_parser("dashboard")
    dashboard.add_argument("--json", action="store_true")
    dashboard.add_argument("--plain", action="store_true")
    dashboard.add_argument("--view", choices=VIEWS, default=None)
    dashboard.add_argument(
        "--refresh-seconds",
        type=float,
        default=2.0,
        help="Refresh cadence for the interactive dashboard. Default: 2.0 seconds.",
    )

    alert = subcommands.add_parser("alert")
    alert_sub = alert.add_subparsers(dest="alert_command", required=True)
    alert_sub.add_parser("list")
    alert_audit = alert_sub.add_parser(
        "audit",
        help="Create alerts for drift, datastore failures, and repeated task failures.",
    )
    alert_audit.add_argument(
        "--failure-threshold",
        type=int,
        default=5,
        help="Consecutive failures before alerting. Default: 5.",
    )
    alert_audit.add_argument(
        "--include-health",
        action="store_true",
        help="Also check configured datastores and alert on failures.",
    )

    chat = subcommands.add_parser(
        "chat",
        help="Send or inspect chat messages.",
        description=(
            "Send a message to an agent or list stored chat history. "
            "Channels are free-form conversation ids such as 'user:alice' or 'team:ops'."
        ),
    )
    chat_sub = chat.add_subparsers(dest="chat_command", required=True, metavar="command")
    chat_send = chat_sub.add_parser("send", help="Send one chat message.")
    chat_send.add_argument(
        "--channel",
        required=True,
        help="Conversation id, for example 'user:alice' or 'team:ops'.",
    )
    chat_send.add_argument("--sender", required=True, help="Identity sending the message.")
    chat_send.add_argument("--recipient", required=True, help="Target agent or user id.")
    chat_send.add_argument("--message", required=True, help="Message body to store and deliver.")
    chat_ask_agent = chat_sub.add_parser(
        "ask-agent",
        help="Ask one agent to answer another agent synchronously.",
        description=(
            "Run a bounded 1:1 inter-agent exchange through the harness. "
            "Both sides must be registered agents."
        ),
    )
    chat_ask_agent.add_argument("--from-agent", required=True, help="Agent asking the question.")
    chat_ask_agent.add_argument("--to-agent", required=True, help="Agent that should answer.")
    chat_ask_agent.add_argument("--message", required=True, help="Question or request.")
    chat_ask_agent.add_argument(
        "--channel",
        default=None,
        help="Optional conversation id/channel. Default: generated agent:<from>:<to>:<uuid>.",
    )
    _add_provider_args(chat_ask_agent)
    chat_group = chat_sub.add_parser(
        "group",
        help="Run a bounded pass-the-mic group chat.",
        description=(
            "Run a serialized group conversation. Only one agent speaks per turn, "
            "and the mic passes through the participant list."
        ),
    )
    chat_group.add_argument(
        "--participant",
        action="append",
        required=True,
        help="Agent id to include. Repeat for each participant.",
    )
    chat_group.add_argument("--agenda", required=True, help="Topic for the group chat.")
    chat_group.add_argument(
        "--moderator",
        default="orchestrator",
        help="Moderator label. Default: orchestrator.",
    )
    chat_group.add_argument(
        "--max-turns",
        type=int,
        default=3,
        help="Maximum serialized turns. Default: 3.",
    )
    chat_group.add_argument(
        "--channel",
        default=None,
        help="Optional conversation id/channel. Default: generated group:<uuid>.",
    )
    _add_provider_args(chat_group)
    chat_list = chat_sub.add_parser("list", help="List chat messages.")
    chat_list.add_argument(
        "--channel",
        default=None,
        help="Optional conversation id to filter on. Omit to list every stored message.",
    )
    chat_tui = chat_sub.add_parser("tui", help="Open an interactive user-to-agent chat TUI.")
    chat_tui.add_argument(
        "--agent",
        default=None,
        help="Agent id to chat with. Default: first registered agent.",
    )
    chat_tui.add_argument(
        "--channel",
        default=None,
        help="Optional pinned conversation id. Default: user:<username>:<agent>.",
    )
    chat_tui.add_argument("--plain", action="store_true", help="Render once without curses.")
    chat_tui.add_argument(
        "--refresh-seconds",
        type=float,
        default=1.0,
        help="Refresh cadence for the interactive chat view. Default: 1.0 seconds.",
    )
    _add_provider_args(chat_tui)

    web = subcommands.add_parser("web", help="Run the local authenticated web gateway.")
    web.add_argument("--host", default=None, help="Bind host. Default: configured web_host.")
    web.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port. Default: configured web_port.",
    )

    connector = subcommands.add_parser(
        "connector",
        help="Receive external connector payloads for smoke testing.",
    )
    connector_sub = connector.add_subparsers(dest="connector_command", required=True)
    approvals = connector_sub.add_parser(
        "approvals",
        help="List and decide external connector identity approvals.",
    )
    approvals_sub = approvals.add_subparsers(dest="approvals_command", required=True)
    approvals_list = approvals_sub.add_parser("list")
    approvals_list.add_argument("--provider", default=None)
    approvals_list.add_argument(
        "--status",
        choices=["pending", "approved", "rejected"],
        default=None,
    )
    approvals_approve = approvals_sub.add_parser("approve")
    approvals_approve.add_argument("--provider", required=True)
    approvals_approve.add_argument("--external-user", required=True)
    approvals_approve.add_argument("--username", required=True)
    approvals_approve.add_argument("--reason", default=None)
    approvals_reject = approvals_sub.add_parser("reject")
    approvals_reject.add_argument("--provider", required=True)
    approvals_reject.add_argument("--external-user", required=True)
    approvals_reject.add_argument("--reason", default=None)
    telegram = connector_sub.add_parser("telegram", help="Handle one Telegram update payload.")
    telegram.add_argument("--payload-json", required=True, help="Telegram update JSON payload.")
    telegram.add_argument("--agent", required=True, help="Default recipient agent id.")
    telegram.add_argument(
        "--allow-user",
        action="append",
        default=[],
        help="Allowed Telegram user id. May be repeated. Defaults to BRIGADE_TELEGRAM_ALLOWLIST.",
    )
    google_chat = connector_sub.add_parser(
        "google-chat",
        help="Handle one Google Chat event payload.",
    )
    google_chat.add_argument(
        "--payload-json",
        required=True,
        help="Google Chat event JSON payload.",
    )
    google_chat.add_argument("--agent", required=True, help="Default recipient agent id.")
    google_chat.add_argument(
        "--allow-user",
        action="append",
        default=[],
        help=(
            "Allowed Google Chat user/email. May be repeated. "
            "Defaults to BRIGADE_GOOGLE_CHAT_ALLOWLIST."
        ),
    )

    health = subcommands.add_parser("health")
    health.add_argument("--json", action="store_true")

    knowledge = subcommands.add_parser(
        "knowledge",
        help="Ingest and inspect local knowledge records.",
        description=(
            "Ingest local files into document and chunk records, upload ad hoc files, "
            "or list stored knowledge documents."
        ),
    )
    knowledge_sub = knowledge.add_subparsers(
        dest="knowledge_command",
        required=True,
        metavar="command",
    )
    ingest = knowledge_sub.add_parser("ingest", help="Ingest a local file with explicit metadata.")
    ingest.add_argument("--title", required=True, help="Human-readable document title.")
    ingest.add_argument("--source", required=True, help="Source label or URL for provenance.")
    ingest.add_argument("--type", required=True, help="Document type label.")
    ingest.add_argument("--path", required=True, help="Local file path to ingest.")
    upload = knowledge_sub.add_parser("upload", help="Upload a local file with default metadata.")
    upload.add_argument("--path", required=True, help="Local file path to upload.")
    upload.add_argument("--title", default=None, help="Optional title override.")
    upload.add_argument(
        "--source",
        default="local",
        help="Source label for provenance. Default: local.",
    )
    upload.add_argument("--type", default="upload", help="Document type label. Default: upload.")
    knowledge_sub.add_parser("list", help="List stored knowledge documents.")

    memory = subcommands.add_parser(
        "memory",
        help="Append, curate, and archive agent memory.",
        description=(
            "Manage per-agent daily memory notes, curate active memory, "
            "and archive stale entries into episodic records."
        ),
    )
    memory_sub = memory.add_subparsers(dest="memory_command", required=True, metavar="command")
    memory_append = memory_sub.add_parser("append", help="Append one memory note.")
    memory_append.add_argument("--agent", required=True, help="Agent id to update.")
    memory_append.add_argument("--date", required=True, help="UTC date in YYYYMMDD form.")
    memory_append.add_argument("--note", required=True, help="Note text to append.")
    memory_curate = memory_sub.add_parser("curate", help="Curate active memory for one agent.")
    memory_curate.add_argument("--agent", required=True, help="Agent id to curate.")
    memory_archive = memory_sub.add_parser("archive", help="Archive stale memory for one agent.")
    memory_archive.add_argument("--agent", required=True, help="Agent id to archive.")
    memory_archive.add_argument(
        "--retention-days",
        type=int,
        default=7,
        help="Archive entries older than this many days. Default: 7.",
    )

    model = subcommands.add_parser(
        "model",
        help="Run one-off model completions for provider testing.",
        description=(
            "Run one-off model completions for provider testing and smoke checks. "
            "This does not create tasks, run agents, or advance the orchestrator."
        ),
        epilog=(
            "Examples:\n"
            "  brigade model complete --prompt \"Summarize the mission\"\n"
            "  brigade model complete --provider ollama --model gpt-oss:20b "
            "--prompt \"Summarize the mission\""
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    model_sub = model.add_subparsers(dest="model_command", required=True, metavar="command")
    model_auth = model_sub.add_parser("auth", help="Manage model provider credentials.")
    model_auth_sub = model_auth.add_subparsers(dest="model_auth_command", required=True)
    model_auth_login = model_auth_sub.add_parser("login")
    model_auth_login.add_argument(
        "--provider",
        choices=sorted(MODEL_AUTH_PROVIDERS),
        required=True,
    )
    model_auth_login.add_argument("--method", choices=["oauth"], required=True)
    model_auth_login.add_argument("--access-token", default=None)
    model_auth_login.add_argument(
        "--access-token-stdin",
        action="store_true",
        help="Read the OAuth access token from stdin instead of the command line.",
    )
    model_auth_login.add_argument("--refresh-token", default=None)
    model_auth_login.add_argument("--expires-at", default=None)
    model_auth_login.add_argument("--expires-in", type=int, default=None)
    model_auth_login.add_argument("--client-id", default=None)
    model_auth_login.add_argument("--client-secret", default=None)
    model_auth_login.add_argument("--scope", default=None)
    model_auth_login.add_argument("--account", default=None)
    model_auth_login.add_argument("--auth-code", default=None)
    model_auth_login.add_argument("--redirect-uri", default=None)
    model_auth_login.add_argument(
        "--token-url",
        default=None,
        help="OAuth token endpoint. Defaults to Google's token endpoint for Gemini.",
    )
    model_auth_login.add_argument(
        "--token-json",
        default=None,
        help="Path to a provider OAuth token JSON file to import.",
    )
    model_auth_status = model_auth_sub.add_parser("status")
    model_auth_status.add_argument(
        "--provider",
        choices=sorted(MODEL_AUTH_PROVIDERS),
        default=None,
    )
    model_auth_logout = model_auth_sub.add_parser("logout")
    model_auth_logout.add_argument(
        "--provider",
        choices=sorted(MODEL_AUTH_PROVIDERS),
        required=True,
    )
    complete = model_sub.add_parser("complete", help="Run one completion request.")
    _add_provider_args(complete, require_prompt=True)
    probe = model_sub.add_parser(
        "probe",
        help="Probe provider model inventories and update the cached model list.",
    )
    probe.add_argument(
        "--provider",
        action="append",
        choices=["ollama", "litellm", "openai", "openai-codex", "anthropic", "gemini"],
        default=None,
        help="Provider to probe. Repeat for multiple. Default: configured providers.",
    )
    route = model_sub.add_parser(
        "route",
        help="Recommend a provider/model for a task using cost and in-flight cloud state.",
    )
    route.add_argument("--task-type", required=True, help="Work type, for example research.")
    route.add_argument(
        "--risk",
        choices=["low", "normal", "high", "critical"],
        default="normal",
        help="Task risk. Default: normal.",
    )
    route.add_argument(
        "--prefer",
        choices=["auto", "local", "cloud"],
        default="auto",
        help="Routing preference. Default: auto.",
    )
    route.add_argument(
        "--local-model",
        default="gpt-oss:20b",
        help="Model to recommend for local Ollama work. Default: gpt-oss:20b.",
    )
    route.add_argument(
        "--cloud-model",
        default="gpt-4.1-mini",
        help="Model to recommend for cloud LiteLLM work. Default: gpt-4.1-mini.",
    )

    cloud = subcommands.add_parser(
        "cloud",
        help="Queue and inspect extended cloud-dispatch work.",
        description="Create explicit cloud-dispatch records for longer prototype work.",
    )
    cloud_sub = cloud.add_subparsers(dest="cloud_command", required=True, metavar="command")
    cloud_dispatch = cloud_sub.add_parser("dispatch", help="Queue one extended cloud job.")
    cloud_dispatch.add_argument("--agent", required=True, help="Agent id to receive the work.")
    cloud_dispatch.add_argument("--assignment", required=True, help="Extended work request.")
    cloud_dispatch.add_argument(
        "--provider",
        default="litellm",
        choices=["litellm"],
        help="Cloud provider adapter. Default: litellm.",
    )
    cloud_dispatch.add_argument(
        "--model",
        default="gpt-4.1-mini",
        help="Cloud model label. Default: gpt-4.1-mini.",
    )
    cloud_dispatch.add_argument(
        "--max-cost-usd",
        type=float,
        default=None,
        help="Optional operator budget cap for the queued job.",
    )
    cloud_list = cloud_sub.add_parser("list", help="List cloud jobs.")
    cloud_list.add_argument(
        "--status",
        default=None,
        help="Optional cloud job status filter, for example queued or failed.",
    )
    cloud_resolve = cloud_sub.add_parser(
        "resolve",
        help="Mark a queued/running cloud job complete or failed.",
    )
    cloud_resolve.add_argument("--job-id", required=True, help="Cloud job id.")
    cloud_resolve.add_argument(
        "--status",
        choices=["complete", "failed"],
        required=True,
        help="Terminal status to apply.",
    )
    cloud_resolve.add_argument(
        "--summary",
        default="resolved by operator",
        help="Resolution summary. Default: resolved by operator.",
    )

    org = subcommands.add_parser(
        "org",
        help="Inspect and persist organization graph snapshots.",
    )
    org_sub = org.add_subparsers(dest="org_command", required=True, metavar="command")
    org_graph = org_sub.add_parser("graph", help="Show the current organization graph.")
    org_graph.add_argument(
        "--persist",
        action="store_true",
        help="Persist this graph snapshot as a provenance record.",
    )

    orchestrator = subcommands.add_parser(
        "orchestrator",
        help="Run one cycle or a continuous orchestration loop.",
        description=(
            "Advance assignment state once with a single cycle, or run the "
            "continuous orchestrator loop."
        ),
        epilog=(
            "Examples:\n"
            "  brigade orchestrator cycle\n"
            "  brigade orchestrator daemon --max-cycles 10\n"
            "  brigade orchestrator daemon --sleep-seconds 30"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    orchestrator_sub = orchestrator.add_subparsers(
        dest="orchestrator_command",
        required=True,
        metavar="command",
    )
    orchestrator_sub.add_parser("cycle", help="Run one orchestrator cycle.")
    stalled = orchestrator_sub.add_parser(
        "propose-stalled-goals",
        help="Create queued assignments for goals that have no active work.",
    )
    stalled.add_argument(
        "--agent",
        default=None,
        help="Optional agent id to evaluate. Default: all agents with goals.",
    )
    daemon = orchestrator_sub.add_parser("daemon", help="Run the continuous orchestrator loop.")
    daemon.add_argument("--max-cycles", type=int, default=None)
    daemon.add_argument(
        "--sleep-seconds",
        type=float,
        default=None,
        help="Seconds to sleep between cycles. Default: configured orchestrator cadence.",
    )
    daemon.add_argument(
        "--no-run-agents",
        action="store_true",
        help="Only advance orchestration state; do not execute assigned agents after each cycle.",
    )
    _add_provider_args(daemon)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        try:
            return _main(None)
        except (PermissionError, RuntimeError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return _main(argv)


def _main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    normalized_args = _normalize_global_args(argv)
    args = parser.parse_args(normalized_args)
    live_command = _live_chat_tui_command(args, normalized_args, cwd=Path.cwd())
    if live_command is not None:
        os.execv(live_command[0], live_command)
    settings = load_settings()
    configure_json_logging(settings.log_level)

    if args.command == "config" and args.config_command in {"show", "inspect"}:
        payload = (
            build_settings_payload(settings)
            if args.config_command == "inspect"
            else {
                "config_path": str(settings.config_path),
                "data_dir": str(settings.data_dir),
                "log_level": settings.log_level,
                "orchestrator_cadence_seconds": settings.orchestrator_cadence_seconds,
                "jwt_issuer": settings.jwt_issuer,
                "jwt_audience": settings.jwt_audience,
                "require_auth": settings.require_auth,
                "qdrant_configured": bool(settings.qdrant_url),
                "neo4j_configured": bool(settings.neo4j_http_url or settings.neo4j_uri),
                "web_host": settings.web_host,
                "web_port": settings.web_port,
                "default_provider": settings.default_provider,
                "default_model": settings.default_model,
                "ollama_base_url": settings.ollama_base_url,
                "store_backend": "PostgresStateStore"
                if settings.postgres_dsn
                else "unconfigured",
                "postgres_required": True,
            }
        )
        print(
            json.dumps(
                payload,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.command == "config" and args.config_command == "set":
        try:
            result = set_config_value(
                settings.config_path,
                args.key,
                args.value,
                base_hash=args.base_hash,
            )
        except ValueError as exc:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "reason": str(exc),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 1
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "auth" and args.auth_command == "verify":
        result = verify_token(settings, args.token_value)
        print(
            json.dumps(
                {
                    "ok": result.ok,
                    "method": result.method,
                    "claims": result.claims,
                    "reason": result.reason,
                    "user": result.user.to_dict() if result.user else None,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if result.ok else 1

    if args.command == "db" and args.db_command == "schema":
        print(combined_schema_sql())
        return 0

    if args.command == "db" and args.db_command == "migrations":
        print(
            json.dumps(
                [str(migration.path) for migration in load_migrations()],
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.command == "db" and args.db_command == "status":
        if not settings.postgres_dsn:
            print(
                json.dumps(
                    {
                        "store_backend": "unconfigured",
                        "ok": False,
                        "reason": (
                            "Postgres is required and is not configured; "
                            "migrations are disabled."
                        ),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 1
        try:
            status_payload = migration_status(settings.postgres_dsn)
        except RuntimeError as exc:
            print(
                json.dumps(
                    {
                        "store_backend": "PostgresStateStore",
                        "ok": False,
                        "reason": str(exc),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 1
        print(json.dumps(status_payload, indent=2, sort_keys=True))
        return 0

    if args.command == "db" and args.db_command == "migrate":
        if not settings.postgres_dsn:
            raise RuntimeError("Postgres is not configured; cannot run migrations")
        try:
            report = apply_migrations(settings.postgres_dsn)
        except MigrationApplyError as exc:
            print(json.dumps(exc.report.to_dict(), indent=2, sort_keys=True))
            return 1
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return 0 if not report.warnings else 1

    if args.command == "datastore" and args.datastore_command == "inspect":
        if args.backend == "redis":
            payload = RedisRuntimeClient(settings.redis_url).inspect(limit=args.limit)
        elif args.backend == "qdrant":
            payload = QdrantEpisodeStore(
                settings.qdrant_url,
                collection=settings.qdrant_collection,
                embedding_base_url=settings.ollama_embedding_base_url,
                embedding_model=settings.ollama_embedding_model,
                embedding_vector_size=settings.ollama_embedding_vector_size,
            ).inspect(limit=args.limit)
        else:
            payload = Neo4jProvenanceStore(
                settings.neo4j_http_url,
                settings.neo4j_auth,
            ).inspect(limit=args.limit)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload.get("ok") else 1

    if args.command == "health":
        checks = check_configured_datastores(settings)
        db_status: dict[str, object] | None = None
        if settings.postgres_dsn:
            try:
                db_status = migration_status(settings.postgres_dsn)
            except Exception as exc:
                checks.append(HealthCheck("migrations", False, str(exc)))
        payload = {
            "checks": [item.__dict__ for item in checks],
            "migration_status": db_status,
            "ok": all(item.ok for item in checks),
        }
        print(
            json.dumps(payload, indent=2, sort_keys=True) if args.json else _format_health(checks)
        )
        return 0 if payload["ok"] else 1

    if args.command == "model" and args.model_command == "auth":
        return _model_auth_command(args, settings)

    if args.command == "model" and args.model_command == "complete":
        provider = _provider_from_args(args, settings)
        try:
            response = provider.complete(args.prompt)
        except ProviderAuthError as exc:
            print(
                json.dumps(
                    {"ok": False, "reason": str(exc)},
                    indent=2,
                    sort_keys=True,
                )
            )
            return 1
        print(json.dumps(response.__dict__, indent=2, sort_keys=True))
        return 0

    if args.command == "web":
        from brigade.web import run_web

        run_web(settings, host=args.host or settings.web_host, port=args.port or settings.web_port)
        return 0

    _guard_host_state(settings, args)
    store = open_state_store(settings)
    actor = _resolve_actor(store, settings, args)

    if args.command == "connector" and args.connector_command == "approvals":
        user = _require_permission(store, settings, actor, "admin")
        decided_by = user.username if user else "bootstrap"
        if args.approvals_command == "list":
            records = store.external_identities(
                provider=args.provider,
                status=args.status,
            )
            print(json.dumps(records, indent=2, sort_keys=True))
            return 0
        if args.approvals_command == "approve":
            record = approve_external_identity(
                store,
                provider=args.provider,
                external_user_id=args.external_user,
                username=args.username,
                decided_by=decided_by,
                reason=args.reason,
            )
            print(json.dumps(record, indent=2, sort_keys=True))
            return 0
        if args.approvals_command == "reject":
            record = reject_external_identity(
                store,
                provider=args.provider,
                external_user_id=args.external_user,
                decided_by=decided_by,
                reason=args.reason,
            )
            print(json.dumps(record, indent=2, sort_keys=True))
            return 0

    if args.command == "connector" and args.connector_command == "telegram":
        _require_permission(store, settings, actor, "chat:write")
        if _find_agent(store, args.agent) is None:
            raise ValueError(f"unknown agent: {args.agent}")
        allowlist = set(args.allow_user) if args.allow_user else parse_allowlist(
            settings.telegram_allowlist
        )
        result = handle_telegram_update(
            store,
            json.loads(args.payload_json),
            default_agent=args.agent,
            allowlist=allowlist,
        )
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0 if result.status == "accepted" else 1

    if args.command == "connector" and args.connector_command == "google-chat":
        _require_permission(store, settings, actor, "chat:write")
        if _find_agent(store, args.agent) is None:
            raise ValueError(f"unknown agent: {args.agent}")
        allowlist = set(args.allow_user) if args.allow_user else parse_allowlist(
            settings.google_chat_allowlist
        )
        result = handle_google_chat_event(
            store,
            json.loads(args.payload_json),
            default_agent=args.agent,
            allowlist=allowlist,
        )
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0 if result.status == "accepted" else 1

    if args.command == "auth" and args.auth_command == "issue":
        _require_permission(store, settings, actor, "auth:write")
        user = _find_user(store, args.username)
        if user is None:
            if args.role is None:
                raise ValueError(
                    "unknown user: "
                    f"{args.username}; create it first with "
                    f"'brigade user add --username {args.username} --role operator' "
                    "or issue a token with --role owner|operator|observer"
                )
            user = User(username=args.username, role=Role(args.role))
            store.add_user(user)
        token = issue_token(settings, user, ttl_seconds=args.ttl_seconds)
        print(json.dumps({"token": token}, indent=2, sort_keys=True))
        return 0

    if args.command == "init" and args.init_command == "mvp":
        _require_permission(store, settings, actor, "admin", allow_bootstrap=True)
        _bootstrap_mvp(store, settings.data_dir, args.mission, force=args.force)
        print(json.dumps({"status": "initialized", "agents": 3}, indent=2, sort_keys=True))
        return 0

    if args.command == "user" and args.user_command == "add":
        _require_permission(store, settings, actor, "user:write", allow_bootstrap=True)
        user = User(username=args.username, role=Role(args.role))
        store.add_user(user)
        print(json.dumps(user.to_dict(), indent=2, sort_keys=True))
        return 0

    if args.command == "user" and args.user_command == "list":
        _require_permission(store, settings, actor, "user:read")
        print(json.dumps([item.to_dict() for item in store.users()], indent=2, sort_keys=True))
        return 0

    if args.command == "mission" and args.mission_command == "set":
        _require_permission(store, settings, actor, "mission:write")
        mission = Mission(
            statement=args.statement,
            success_criteria=args.success,
            explicitly_not=args.explicitly_not,
        )
        store.set_mission(mission)
        print(json.dumps(mission.to_dict(), indent=2, sort_keys=True))
        return 0

    if args.command == "mission" and args.mission_command == "show":
        _require_permission(store, settings, actor, "mission:read")
        mission = store.mission()
        print(json.dumps(mission.to_dict() if mission else None, indent=2, sort_keys=True))
        return 0

    if args.command == "agent" and args.agent_command == "add":
        _require_permission(store, settings, actor, "agent:write")
        agent = Agent(
            agent_id=args.id,
            display_name=args.name,
            workspace_path=args.workspace,
            role=args.role,
        )
        store.add_agent(agent)
        ensure_agent_workspace(agent, settings.data_dir)
        print(json.dumps(agent.to_dict(), indent=2, sort_keys=True))
        return 0

    if args.command == "agent" and args.agent_command == "onboard":
        _require_permission(store, settings, actor, "agent:write")
        workspace = args.workspace or f"workspace-{args.id}"
        if args.crew_chief and not args.team:
            raise ValueError("--crew-chief requires --team")
        team = _find_team(store, args.team) if args.team else None
        if args.team and team is None:
            if not args.create_team:
                raise ValueError(f"unknown team: {args.team}; rerun with --create-team")
            team = Team(team_id=args.team, display_name=args.team)
            store.upsert_team(team)
        agent = Agent(
            agent_id=args.id,
            display_name=args.name,
            workspace_path=workspace,
            role=args.role,
            team_id=args.team,
            model_provider=args.provider or settings.default_provider,
            model_name=args.model or settings.default_model,
            specialties=[item.strip() for item in args.specialties if item.strip()],
        )
        store.add_agent(agent)
        ensure_agent_workspace(agent, settings.data_dir)
        if team is not None:
            team = _team_with_member(team, args.id, crew_chief=args.crew_chief)
            store.upsert_team(team)
        diagnostics = validate_agent_workspace(agent, settings.data_dir)
        print(
            json.dumps(
                {
                    "agent": agent.to_dict(),
                    "team": team.to_dict() if team else None,
                    "workspace": str(settings.data_dir / agent.workspace_path),
                    "diagnostics": [item.to_dict() for item in diagnostics],
                    "valid": not any(item.severity == "error" for item in diagnostics),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.command == "model" and args.model_command == "probe":
        _require_permission(store, settings, actor, "admin")
        inventory = probe_model_inventory(settings, providers=args.provider)
        store.set_model_inventory(inventory)
        print(json.dumps(inventory, indent=2, sort_keys=True))
        return 0

    if args.command == "agent" and args.agent_command == "model":
        _require_permission(store, settings, actor, "agent:write")
        agent = _find_agent(store, args.id)
        if agent is None:
            raise ValueError(f"unknown agent: {args.id}")
        updated = replace(
            agent,
            model_provider=args.provider,
            model_name=args.model,
        )
        store.add_agent(updated)
        print(json.dumps(updated.to_dict(), indent=2, sort_keys=True))
        return 0

    if args.command == "agent" and args.agent_command == "update":
        _require_permission(store, settings, actor, "agent:write")
        agent = _find_agent(store, args.id)
        if agent is None:
            raise ValueError(f"unknown agent: {args.id}")
        updates: dict[str, Any] = {}
        if args.role is not None:
            updates["role"] = args.role
        if args.specialties is not None:
            updates["specialties"] = [
                item.strip() for item in args.specialties if item.strip()
            ]
        if not updates:
            raise ValueError("nothing to update: pass --role and/or --specialty")
        updated = replace(agent, **updates)
        store.add_agent(updated)
        print(json.dumps(updated.to_dict(), indent=2, sort_keys=True))
        return 0

    if args.command == "agent" and args.agent_command == "validate":
        _require_permission(store, settings, actor, "status:read")
        agent = _find_agent(store, args.id)
        if agent is None:
            raise ValueError(f"unknown agent: {args.id}")
        if args.repair:
            _require_permission(store, settings, actor, "agent:write")
            ensure_agent_workspace(agent, settings.data_dir)
        diagnostics = validate_agent_workspace(agent, settings.data_dir)
        print(
            json.dumps(
                {
                    "agent_id": agent.agent_id,
                    "workspace": str(settings.data_dir / agent.workspace_path),
                    "diagnostics": [item.to_dict() for item in diagnostics],
                    "valid": not any(item.severity == "error" for item in diagnostics),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.command == "agent" and args.agent_command == "list":
        _require_permission(store, settings, actor, "status:read")
        print(json.dumps([item.to_dict() for item in store.agents()], indent=2, sort_keys=True))
        return 0

    if args.command == "team" and args.team_command == "create":
        _require_permission(store, settings, actor, "team:write")
        if args.parent and _find_team(store, args.parent) is None:
            raise ValueError(f"unknown parent team: {args.parent}")
        if args.escalation_team and _find_team(store, args.escalation_team) is None:
            raise ValueError(f"unknown escalation team: {args.escalation_team}")
        existing = _find_team(store, args.id)
        team = Team(
            team_id=args.id,
            display_name=args.name,
            description=args.description,
            parent_team_id=args.parent,
            crew_chief_id=existing.crew_chief_id if existing else None,
            members=existing.members if existing else [],
            delegation_policy=args.delegation_policy,
            escalation_team_id=args.escalation_team
            if args.escalation_team is not None
            else existing.escalation_team_id
            if existing
            else None,
            created_at=existing.created_at if existing else Team(args.id, args.name).created_at,
        )
        store.upsert_team(team)
        print(json.dumps(team.to_dict(), indent=2, sort_keys=True))
        return 0

    if args.command == "team" and args.team_command == "list":
        _require_permission(store, settings, actor, "team:read")
        print(json.dumps([item.to_dict() for item in store.teams()], indent=2, sort_keys=True))
        return 0

    if args.command == "team" and args.team_command == "show":
        _require_permission(store, settings, actor, "team:read")
        teams = store.teams()
        if args.id:
            team = next((item for item in teams if item.team_id == args.id), None)
            if team is None:
                raise ValueError(f"unknown team: {args.id}")
            print(json.dumps(_team_view(team, store), indent=2, sort_keys=True))
        else:
            print(json.dumps([_team_view(item, store) for item in teams], indent=2, sort_keys=True))
        return 0

    if args.command == "team" and args.team_command == "status":
        _require_permission(store, settings, actor, "team:read")
        print(json.dumps(_team_status_view(store, args.team), indent=2, sort_keys=True))
        return 0

    if args.command == "team" and args.team_command == "assign":
        _require_permission(store, settings, actor, "team:write")
        team = _find_team(store, args.team)
        if team is None:
            raise ValueError(f"unknown team: {args.team}")
        agent = _find_agent(store, args.agent)
        if agent is None:
            raise ValueError(f"unknown agent: {args.agent}")
        team = _team_with_member(team, args.agent, crew_chief=args.crew_chief)
        store.upsert_team(team)
        store.add_agent(_agent_with_team(agent, args.team))
        print(json.dumps(_team_view(team, store), indent=2, sort_keys=True))
        return 0

    if args.command == "team" and args.team_command == "chief":
        _require_permission(store, settings, actor, "team:write")
        team = _find_team(store, args.team)
        if team is None:
            raise ValueError(f"unknown team: {args.team}")
        agent = _find_agent(store, args.agent)
        if agent is None:
            raise ValueError(f"unknown agent: {args.agent}")
        team = _team_with_member(team, args.agent, crew_chief=True)
        store.upsert_team(team)
        store.add_agent(_agent_with_team(agent, args.team))
        print(json.dumps(_team_view(team, store), indent=2, sort_keys=True))
        return 0

    if args.command == "team" and args.team_command == "policy":
        _require_permission(store, settings, actor, "team:write")
        team = _find_team(store, args.team)
        if team is None:
            raise ValueError(f"unknown team: {args.team}")
        if args.escalation_team and _find_team(store, args.escalation_team) is None:
            raise ValueError(f"unknown escalation team: {args.escalation_team}")
        updated = _team_with_policy(
            team,
            delegation_policy=args.delegation_policy,
            escalation_team_id=args.escalation_team,
        )
        store.upsert_team(updated)
        _persist_org_graph(store, reason=f"policy updated for {args.team}")
        print(json.dumps(_team_view(updated, store), indent=2, sort_keys=True))
        return 0

    if args.command == "team" and args.team_command == "delegate":
        current_user = _require_permission(store, settings, actor, "task:write")
        result = _delegate_from_crew_chief(
            store,
            team_id=args.team,
            chief_agent_id=args.chief,
            target_agent_id=args.agent,
            assignment_text=args.assignment,
            goal_statement=args.goal_statement,
            rationale=args.rationale,
            priority=Priority(args.priority),
            current_user=current_user,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "team" and args.team_command == "route-work":
        current_user = _require_permission(store, settings, actor, "task:write")
        result = _route_team_work(
            store,
            team_id=args.team,
            assignment_text=args.assignment,
            scope=args.scope,
            urgency=args.urgency,
            goal_statement=args.goal_statement,
            current_user=current_user,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "team" and args.team_command == "escalate":
        current_user = _require_permission(store, settings, actor, "task:write")
        result = _escalate_team_work(
            store,
            from_team_id=args.from_team,
            to_team_id=args.to_team,
            chief_agent_id=args.chief,
            assignment_text=args.assignment,
            reason=args.reason,
            current_user=current_user,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "agent" and args.agent_command == "run":
        _require_permission(store, settings, actor, "orchestrator:write")
        default_provider = _provider_from_args(args, settings)
        provider = (
            default_provider
            if _provider_args_explicit(args)
            else _provider_for_agent(settings, store, args.id)
        )
        try:
            result = run_agent_once(args.id, store, provider)
        except RuntimeError as exc:
            if _provider_args_explicit(args) or _provider_identity(provider) == _provider_identity(
                default_provider
            ) or is_local_inference_backpressure(exc):
                raise
            store.add_alert(
                f"agent {args.id} provider failed; retrying with default provider"
            )
            result = run_agent_once(args.id, store, default_provider)
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0

    if args.command == "agent" and args.agent_command == "run-all":
        _require_permission(store, settings, actor, "orchestrator:write")
        provider = _provider_from_args(args, settings)
        provider_factory = _managed_agent_provider_factory(args, settings, store)
        results = run_managed_agents(
            store,
            provider,
            provider_factory=provider_factory,
            fallback_provider=provider if provider_factory else None,
        )
        print(json.dumps([item.to_dict() for item in results], indent=2, sort_keys=True))
        return 0

    if args.command == "goal" and args.goal_command == "add":
        _require_permission(store, settings, actor, "goal:write")
        goal = Goal(
            statement=args.statement,
            success_criteria=args.success,
            explicitly_not=args.explicitly_not,
            set_by=args.set_by,
            human_confirmed=args.human_confirmed,
            engagement_mode=args.engagement_mode,
        )
        store.add_goal(args.agent, goal)
        print(json.dumps(goal.to_dict(), indent=2, sort_keys=True))
        return 0

    if args.command == "goal" and args.goal_command == "list":
        _require_permission(store, settings, actor, "goal:read")
        payload = {
            key: [goal.to_dict() for goal in values]
            for key, values in store.goals(args.agent).items()
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "export" and args.export_command == "training-data":
        _require_permission(store, settings, actor, "export:read")
        manifest = export_training_data(
            store,
            out_dir=Path(args.out),
            since=args.since,
        )
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0

    if args.command == "proposal" and args.proposal_command == "list":
        _require_permission(store, settings, actor, "proposal:read")
        print(
            json.dumps(
                store.proposals(kind=args.kind, status=args.status),
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.command == "proposal" and args.proposal_command in {"approve", "reject"}:
        current_user = _require_permission(store, settings, actor, "proposal:write")
        decision = "approved" if args.proposal_command == "approve" else "rejected"
        result = decide_proposal(
            store,
            proposal_id=args.proposal_id,
            decision=decision,
            decided_by=current_user.username if current_user else "anonymous",
            reason=getattr(args, "reason", None),
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "task" and args.task_command == "create":
        current_user = _require_permission(store, settings, actor, "task:write")
        _require_known_agent(store, args.agent)
        _validate_agent_assignment_authority(
            store,
            creator_agent_id=args.created_by,
            target_agent_id=args.agent,
            source=args.source,
        )
        assignment = Assignment(
            assignment=args.assignment,
            assigned_to=args.agent,
            created_by=args.created_by,
            source=args.source,
            priority=Priority(args.priority),
            work_mode=WorkMode(args.work_mode),
            kind=AssignmentKind(args.kind),
            estimated_cycles=args.estimated_cycles,
            dependency_ids=args.depends_on,
            goal_statement=args.goal_statement,
            assignment_rationale=args.rationale,
            created_by_user_id=current_user.username if current_user else None,
            created_by_role=current_user.role.value if current_user else None,
            idempotency_key=args.idempotency_key,
        )
        persisted = store.add_assignment(assignment)
        print(json.dumps(persisted.to_dict(), indent=2, sort_keys=True))
        return 0

    if args.command == "task" and args.task_command == "prompt":
        _require_permission(store, settings, actor, "task:write")
        payload = _interactive_task_prompt(store, actor.user if actor.user else None)
        namespace = argparse.Namespace(**payload)
        namespace.priority = payload["priority"]
        namespace.work_mode = payload["work_mode"]
        namespace.depends_on = payload["depends_on"]
        namespace.goal_statement = payload["goal_statement"]
        namespace.rationale = payload["rationale"]
        namespace.idempotency_key = payload["idempotency_key"]
        namespace.created_by = payload["created_by"]
        namespace.source = payload["source"]
        namespace.estimated_cycles = payload["estimated_cycles"]
        namespace.agent = payload["agent"]
        namespace.assignment = payload["assignment"]
        _require_known_agent(store, namespace.agent)
        current_user = actor.user
        assignment = Assignment(
            assignment=namespace.assignment,
            assigned_to=namespace.agent,
            created_by=namespace.created_by,
            source=namespace.source,
            priority=Priority(namespace.priority),
            work_mode=WorkMode(namespace.work_mode),
            estimated_cycles=namespace.estimated_cycles,
            dependency_ids=namespace.depends_on,
            goal_statement=namespace.goal_statement,
            assignment_rationale=namespace.rationale,
            created_by_user_id=current_user.username if current_user else None,
            created_by_role=current_user.role.value if current_user else None,
            idempotency_key=namespace.idempotency_key,
        )
        persisted = store.add_assignment(assignment)
        print(json.dumps(persisted.to_dict(), indent=2, sort_keys=True))
        return 0

    if args.command == "task" and args.task_command == "list":
        _require_permission(store, settings, actor, "task:read")
        tasks = store.assignments()
        if args.agent:
            tasks = [item for item in tasks if item.assigned_to == args.agent]
        if args.status:
            tasks = [item for item in tasks if item.status.value == args.status]
        print(json.dumps([item.to_dict() for item in tasks], indent=2, sort_keys=True))
        return 0

    if args.command == "task" and args.task_command == "inspect":
        _require_permission(store, settings, actor, "task:read")
        print(json.dumps(_inspect_assignment(store, args.id), indent=2, sort_keys=True))
        return 0

    if args.command == "task" and args.task_command == "cancel":
        _require_permission(store, settings, actor, "task:write")
        actor_label = actor.user.username if actor.user else "cli"
        try:
            result = cancel_assignment(
                store, args.id, reason=args.reason, by=actor_label, force=args.force
            )
        except AssignmentActionError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "task" and args.task_command == "reissue":
        _require_permission(store, settings, actor, "task:write")
        actor_label = actor.user.username if actor.user else "cli"
        try:
            result = reissue_assignment(store, args.id, by=actor_label)
        except AssignmentActionError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "task" and args.task_command == "cancel-all":
        _require_permission(store, settings, actor, "task:write")
        actor_label = actor.user.username if actor.user else "cli"
        if args.dry_run:
            matches: list[dict[str, Any]] = []
            for assignment in store.assignments():
                if assignment.status in TERMINAL_STATUSES:
                    continue
                if args.status is not None and assignment.status.value != args.status:
                    continue
                if args.kind is not None and assignment.kind.value != args.kind:
                    continue
                if args.blocker is not None and not any(
                    args.blocker.lower() in (b or "").lower() for b in assignment.blockers
                ):
                    continue
                matches.append(
                    {
                        "assignment_id": assignment.assignment_id,
                        "status": assignment.status.value,
                        "kind": assignment.kind.value,
                        "assigned_to": assignment.assigned_to,
                        "blockers": assignment.blockers,
                    }
                )
            print(
                json.dumps(
                    {"dry_run": True, "would_cancel": matches, "count": len(matches)},
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        results = cancel_assignments_where(
            store,
            status=args.status,
            blocker_contains=args.blocker,
            kind=args.kind,
            reason=args.reason,
            by=actor_label,
        )
        print(
            json.dumps(
                {"cancelled": results, "count": len(results)}, indent=2, sort_keys=True
            )
        )
        return 0

    if args.command == "status":
        _require_permission(store, settings, actor, "status:read")
        payload = _status_payload(store)
        print(
            json.dumps(payload, indent=2, sort_keys=True)
            if args.json
            else _format_status(payload)
        )
        return 0

    if args.command == "dashboard":
        _require_permission(store, settings, actor, "status:read")
        if args.json:
            payload = build_dashboard_payload(store)
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        if not args.plain:
            try:
                return run_dashboard_tui(
                    lambda: build_dashboard_payload(store),
                    refresh_seconds=args.refresh_seconds,
                )
            except RuntimeError:
                pass
        if args.view:
            print(render_dashboard_view(build_dashboard_payload(store), args.view))
        else:
            print(_format_dashboard_summary(store))
        return 0

    if args.command == "settings" and args.settings_command == "tui":
        _require_permission(store, settings, actor, "status:read")
        payload = build_settings_payload(settings)
        if not args.plain:
            try:
                return run_settings_tui(
                    lambda: build_settings_payload(settings),
                    refresh_seconds=args.refresh_seconds,
                )
            except RuntimeError:
                pass
        print(render_settings_view(payload))
        return 0

    if args.command == "alert" and args.alert_command == "list":
        _require_permission(store, settings, actor, "status:read")
        print(json.dumps(store.alerts(), indent=2, sort_keys=True))
        return 0

    if args.command == "alert" and args.alert_command == "audit":
        _require_permission(store, settings, actor, "orchestrator:write")
        result = _audit_alerts(
            store,
            settings,
            failure_threshold=args.failure_threshold,
            include_health=args.include_health,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "chat" and args.chat_command == "send":
        _require_permission(store, settings, actor, "chat:write")
        message = ChatMessage(
            channel=args.channel,
            sender=args.sender,
            recipient=args.recipient,
            content=args.message,
            metadata=_chat_metadata(store, actor, args.sender),
        )
        store.add_message(message)
        print(json.dumps(message.to_dict(), indent=2, sort_keys=True))
        return 0

    if args.command == "chat" and args.chat_command == "ask-agent":
        _require_permission(store, settings, actor, "chat:write")
        result = _ask_agent_chat(
            store,
            actor,
            from_agent_id=args.from_agent,
            to_agent_id=args.to_agent,
            content=args.message,
            provider=_provider_from_args(args, settings),
            channel=args.channel,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "chat" and args.chat_command == "group":
        _require_permission(store, settings, actor, "chat:write")
        result = _group_chat(
            store,
            actor,
            participants=args.participant,
            agenda=args.agenda,
            provider=_provider_from_args(args, settings),
            moderator=args.moderator,
            max_turns=args.max_turns,
            channel=args.channel,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "chat" and args.chat_command == "list":
        _require_permission(store, settings, actor, "chat:read")
        print(
            json.dumps(
                [item.to_dict() for item in store.messages(args.channel)],
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.command == "chat" and args.chat_command == "tui":
        current_user = _require_permission(store, settings, actor, "chat:write")
        agent_ids = [agent.agent_id for agent in store.agents()]
        initial_agent = args.agent or (agent_ids[0] if agent_ids else None)
        if initial_agent is None:
            raise ValueError("no agents available; create one or pass --agent")
        if initial_agent not in agent_ids:
            raise ValueError(f"unknown agent: {initial_agent}")
        sender = current_user.username if current_user else "operator"
        chat_provider = _chat_tui_provider_from_args(args, settings)

        def tui_channel(agent_id: str) -> str:
            return args.channel or f"user:{sender}:{agent_id}"

        if args.plain:
            channel = tui_channel(initial_agent)
            payload = build_chat_payload(store, channel=channel)
            print(render_chat_view(payload, channel))
            return 0
        try:
            return run_chat_tui(
                store,
                lambda message, agent_id, channel: send_user_chat(
                    store,
                    actor,
                    user=current_user,
                    agent_id=agent_id or initial_agent,
                    content=message,
                    provider=chat_provider,
                    channel=channel or tui_channel(agent_id or initial_agent),
                    idempotency_key=f"tui:{agent_id or initial_agent}:{uuid4()}",
                ),
                channel=args.channel,
                agent_id=initial_agent,
                agent_ids=agent_ids,
                channel_for_agent=tui_channel,
                refresh_seconds=args.refresh_seconds,
            )
        except RuntimeError:
            channel = tui_channel(initial_agent)
            payload = build_chat_payload(store, channel=channel)
            print(render_chat_view(payload, channel))
            return 0

    if args.command == "knowledge" and args.knowledge_command == "ingest":
        _require_permission(store, settings, actor, "knowledge:write")
        document = _ingest_document(
            store,
            title=args.title,
            source=args.source,
            document_type=args.type,
            path=args.path,
        )
        print(json.dumps(document, indent=2, sort_keys=True))
        return 0

    if args.command == "knowledge" and args.knowledge_command == "upload":
        _require_permission(store, settings, actor, "knowledge:write")
        path = Path(args.path)
        title = args.title or path.stem.replace("-", " ").replace("_", " ").strip() or path.name
        document = _ingest_document(
            store,
            title=title,
            source=args.source,
            document_type=args.type,
            path=str(path),
        )
        print(json.dumps(document, indent=2, sort_keys=True))
        return 0

    if args.command == "knowledge" and args.knowledge_command == "list":
        _require_permission(store, settings, actor, "knowledge:read")
        print(json.dumps(store.knowledge_documents(), indent=2, sort_keys=True))
        return 0

    if args.command == "memory" and args.memory_command == "append":
        _require_permission(store, settings, actor, "memory:write")
        workspace = _workspace_for_agent(store, settings.data_dir, args.agent)
        path = append_daily_memory(workspace, args.date, args.note)
        print(json.dumps({"path": str(path)}, indent=2, sort_keys=True))
        return 0

    if args.command == "memory" and args.memory_command == "curate":
        _require_permission(store, settings, actor, "memory:write")
        workspace = _workspace_for_agent(store, settings.data_dir, args.agent)
        path = curate_workspace_memory(workspace)
        print(
            json.dumps(
                {"path": str(path), "bytes": len(path.read_bytes())}, indent=2, sort_keys=True
            )
        )
        return 0

    if args.command == "memory" and args.memory_command == "archive":
        _require_permission(store, settings, actor, "memory:write")
        workspace = _workspace_for_agent(store, settings.data_dir, args.agent)
        archived = archive_stale_daily_memories(
            workspace,
            agent_id=args.agent,
            retention_days=args.retention_days,
        )
        for episode in archived:
            store.add_episode(episode)
            store.add_provenance_record(
                {
                    "record_id": episode["episode_id"],
                    "node_type": "daily_memory",
                    "node_id": episode["source_id"],
                    "source_refs": episode["source_refs"],
                    "metadata": {"agent_id": args.agent},
                    "created_at": episode["created_at"],
                }
            )
        print(json.dumps(archived, indent=2, sort_keys=True))
        return 0

    if args.command == "model" and args.model_command == "route":
        _require_permission(store, settings, actor, "status:read")
        decision = build_model_routing_decision(
            store,
            task_type=args.task_type,
            risk=args.risk,
            prefer=args.prefer,
            local_model=args.local_model,
            cloud_model=args.cloud_model,
        )
        store.add_orchestrator_reasoning(
            _reasoning_event(
                source="model_route",
                decision_summary=decision["rationale"],
                payload=decision,
            )
        )
        print(json.dumps(decision, indent=2, sort_keys=True))
        return 0

    if args.command == "cloud" and args.cloud_command == "dispatch":
        current_user = _require_permission(store, settings, actor, "task:write")
        result = _dispatch_cloud_job(
            store,
            agent_id=args.agent,
            assignment_text=args.assignment,
            provider=args.provider,
            model=args.model,
            max_cost_usd=args.max_cost_usd,
            current_user=current_user,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "cloud" and args.cloud_command == "list":
        _require_permission(store, settings, actor, "status:read")
        print(json.dumps(store.cloud_jobs(args.status), indent=2, sort_keys=True))
        return 0

    if args.command == "cloud" and args.cloud_command == "resolve":
        _require_permission(store, settings, actor, "task:write")
        result = _resolve_cloud_job(
            store,
            job_id=args.job_id,
            status=args.status,
            summary=args.summary,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "org" and args.org_command == "graph":
        _require_permission(store, settings, actor, "team:read")
        graph = _organization_graph(store)
        if args.persist:
            _persist_org_graph(store, graph=graph, reason="operator snapshot")
        print(json.dumps(graph, indent=2, sort_keys=True))
        return 0

    if args.command == "orchestrator" and args.orchestrator_command == "cycle":
        _require_permission(store, settings, actor, "orchestrator:write")
        result = run_full_cycle(
            store,
            config=OrchestrationConfig.from_settings(settings).with_overrides(
                store.runtime_overrides()
            ),
        )
        print(
            json.dumps(
                {
                    "assigned": [item.assignment_id for item in result.assigned],
                    "skipped": [item.assignment_id for item in result.skipped],
                    "alerts": result.alerts,
                    "cycle_outcome": result.outcome.to_dict(),
                    "sub_results": result.sub_results,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.command == "orchestrator" and args.orchestrator_command == "propose-stalled-goals":
        _require_permission(store, settings, actor, "orchestrator:write")
        result = _propose_stalled_goal_work(store, agent_id=args.agent)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "orchestrator" and args.orchestrator_command == "daemon":
        _require_permission(store, settings, actor, "orchestrator:write")
        max_cycles = args.max_cycles
        sleep_seconds = (
            args.sleep_seconds
            if args.sleep_seconds is not None
            else settings.orchestrator_cadence_seconds
        )
        completed = 0
        agent_results = []
        provider = _provider_from_args(args, settings)
        provider_factory = _managed_agent_provider_factory(args, settings, store)
        model_inventory = probe_model_inventory(settings)
        store.set_model_inventory(model_inventory)
        # Graceful drain: docker stop / compose restarts send SIGTERM. Finish
        # the in-flight cycle or agent run (the handler only sets a flag), stop
        # dispatching, and exit cleanly instead of being killed mid-run and
        # leaving assignments stranded in "working".
        shutdown = threading.Event()

        def _request_drain(signum: int, frame: object) -> None:
            shutdown.set()

        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(signum, _request_drain)
            except ValueError:  # not the main thread (tests)
                break
        while (max_cycles is None or completed < max_cycles) and not shutdown.is_set():
            cycle_config = OrchestrationConfig.from_settings(settings).with_overrides(
                store.runtime_overrides()
            )
            run_full_cycle(
                store,
                provider=provider,
                config=cycle_config,
            )
            if not args.no_run_agents and not shutdown.is_set():
                agent_results.extend(
                    run_managed_agents(
                        store,
                        provider,
                        provider_factory=provider_factory,
                        fallback_provider=provider if provider_factory else None,
                    )
                )
            completed += 1
            if max_cycles is not None and completed >= max_cycles:
                break
            # Event.wait wakes immediately on SIGTERM, unlike time.sleep.
            # Re-read the cadence each cycle so a runtime override from the
            # GUI takes effect without a restart (explicit --sleep-seconds
            # still wins).
            if args.sleep_seconds is None:
                sleep_seconds = cycle_config.cadence_seconds
            if shutdown.wait(sleep_seconds):
                break
        print(
            json.dumps(
                {
                    "cycles": completed,
                    "drained": shutdown.is_set(),
                    "agent_runs": [item.to_dict() for item in agent_results],
                    "model_inventory": model_inventory,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    parser.error("unhandled command")
    return 2


def _normalize_global_args(argv: Sequence[str] | None) -> list[str]:
    raw = list(sys.argv[1:] if argv is None else argv)
    allow_host_state = False
    normalized: list[str] = []
    for item in raw:
        if item == "--allow-host-state":
            allow_host_state = True
            continue
        normalized.append(item)
    if allow_host_state:
        return ["--allow-host-state", *normalized]
    return normalized


def _live_chat_tui_command(
    args: argparse.Namespace,
    argv: Sequence[str],
    *,
    cwd: Path,
    in_container: bool | None = None,
) -> list[str] | None:
    if getattr(args, "allow_host_state", False):
        return None
    running_in_container = Path("/.dockerenv").exists() if in_container is None else in_container
    if running_in_container:
        return None
    if getattr(args, "command", None) != "chat" or getattr(args, "chat_command", None) != "tui":
        return None
    repo_root = _find_repo_root(cwd)
    if repo_root is None:
        return None
    wrapper = repo_root / "ops" / "brigade-live.sh"
    if not wrapper.exists():
        return None
    return [str(wrapper), *argv]


def _add_provider_args(parser: argparse.ArgumentParser, require_prompt: bool = False) -> None:
    parser.add_argument(
        "--provider",
        choices=["ollama", "litellm", "openai", "openai-codex", "anthropic", "gemini"],
        default=None,
        help=(
            "Model provider to use. Default: resolved settings provider "
            "(Ollama unless configured otherwise). openai/anthropic/gemini "
            "are LiteLLM routes."
        ),
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name. Default: resolved settings model.",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=(
            "Provider base URL. Default: BRIGADE_OLLAMA_BASE_URL "
            "or http://127.0.0.1:11434."
        ),
    )
    parser.add_argument("--api-key", default=None, help="Optional API key for provider auth.")
    if require_prompt:
        parser.add_argument("--prompt", required=True)


def _model_auth_command(args: argparse.Namespace, settings: Settings) -> int:
    if args.model_auth_command == "status":
        providers = [args.provider] if args.provider else sorted(MODEL_AUTH_PROVIDERS)
        payload = {
            "secret_store_path": str(settings.secret_store_path or settings.data_dir / "secrets"),
            "providers": [oauth_credential_status(settings, provider) for provider in providers],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.model_auth_command == "logout":
        deleted = delete_oauth_credential(settings, args.provider)
        print(
            json.dumps(
                {"provider": args.provider, "deleted": deleted},
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.model_auth_command == "login":
        token_payload = None
        access_token = args.access_token
        if args.access_token_stdin:
            access_token = sys.stdin.read().strip()
        if args.token_json:
            token_payload = json.loads(Path(args.token_json).read_text(encoding="utf-8"))
        if args.auth_code:
            try:
                token_payload = _exchange_oauth_code(args)
            except ValueError as exc:
                print(json.dumps({"ok": False, "reason": str(exc)}, indent=2, sort_keys=True))
                return 1
        try:
            status = write_oauth_credential(
                settings,
                provider=args.provider,
                access_token=access_token,
                refresh_token=args.refresh_token,
                expires_at=args.expires_at,
                expires_in=args.expires_in,
                client_id=args.client_id,
                client_secret=args.client_secret,
                scope=args.scope,
                account=args.account,
                token_payload=token_payload,
            )
        except ValueError as exc:
            print(json.dumps({"ok": False, "reason": str(exc)}, indent=2, sort_keys=True))
            return 1
        print(json.dumps({"ok": True, "credential": status}, indent=2, sort_keys=True))
        return 0

    raise ValueError(f"unhandled model auth command: {args.model_auth_command}")


def _exchange_oauth_code(args: argparse.Namespace) -> dict[str, Any]:
    token_url = args.token_url
    if token_url is None and args.provider == "gemini":
        token_url = "https://oauth2.googleapis.com/token"
    if token_url is None:
        raise ValueError(
            "--token-url is required for OAuth code exchange with this provider"
        )
    if not args.client_id:
        raise ValueError("--client-id is required for OAuth code exchange")
    if not args.redirect_uri:
        raise ValueError("--redirect-uri is required for OAuth code exchange")

    form = {
        "grant_type": "authorization_code",
        "code": args.auth_code,
        "client_id": args.client_id,
        "redirect_uri": args.redirect_uri,
    }
    if args.client_secret:
        form["client_secret"] = args.client_secret
    data = urllib.parse.urlencode(form).encode("utf-8")
    request = urllib.request.Request(
        token_url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise ValueError(f"OAuth token exchange failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("OAuth token exchange returned a non-object payload")
    payload.setdefault("client_id", args.client_id)
    payload.setdefault("client_secret", args.client_secret)
    payload.setdefault("scope", args.scope)
    payload.setdefault("account", args.account)
    return payload


def _provider_from_args(args: argparse.Namespace, settings: Settings | None = None):
    provider = args.provider or (settings.default_provider if settings else "ollama")
    model = args.model or (settings.default_model if settings else "gpt-oss:20b")
    default_base_url = (
        settings.ollama_base_url
        if settings is not None
        else os.environ.get("BRIGADE_OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    )
    base_url = args.base_url or default_base_url
    api_base = (
        None
        if base_url == default_base_url
        else base_url
    )
    if settings is not None:
        return provider_from_settings(
            settings,
            provider=provider,
            model=model,
            api_key=args.api_key,
            api_base=api_base,
        )
    if provider == "ollama":
        return OllamaProvider(base_url=base_url, model=model)
    if provider in {"openai", "openai-codex"}:
        return LiteLLMProvider(
            model=model,
            api_key=args.api_key or os.environ.get("OPENAI_API_KEY"),
            api_base=api_base,
            provider_name=provider,
        )
    if provider == "anthropic":
        return LiteLLMProvider(
            model=model,
            api_key=args.api_key or os.environ.get("ANTHROPIC_API_KEY"),
            api_base=api_base,
            provider_name="anthropic",
        )
    if provider == "gemini":
        model = model if model.startswith("gemini/") else f"gemini/{model}"
        return LiteLLMProvider(
            model=model,
            api_key=args.api_key or os.environ.get("GEMINI_API_KEY"),
            api_base=api_base,
            provider_name="gemini",
        )
    return LiteLLMProvider(model=model, api_key=args.api_key, api_base=base_url)


def _provider_args_explicit(args: argparse.Namespace) -> bool:
    return bool(args.provider or args.model or args.base_url or args.api_key)


def _provider_for_agent(settings: Settings, store: StateStore, agent_id: str) -> ModelProvider:
    agent = next((item for item in store.agents() if item.agent_id == agent_id), None)
    if agent is None:
        return provider_from_settings(settings)
    return provider_from_settings(
        settings,
        provider=agent.model_provider,
        model=agent.model_name,
    )


def _provider_identity(provider: ModelProvider) -> tuple[str, str, str]:
    return (
        str(getattr(provider, "provider_name", provider.__class__.__name__)),
        str(getattr(provider, "model", "unknown")),
        str(getattr(provider, "route_type", "unknown")),
    )


def _managed_agent_provider_factory(
    args: argparse.Namespace,
    settings: Settings,
    store: StateStore,
) -> Callable[[str], ModelProvider] | None:
    if _provider_args_explicit(args):
        return None
    return lambda agent_id: _provider_for_agent(settings, store, agent_id)


def _chat_tui_provider_from_args(args: argparse.Namespace, settings: Settings):
    if args.provider or args.model or args.base_url or args.api_key:
        return _provider_from_args(args, settings)
    recommended = available_model_options(settings)["recommended"]
    return provider_from_settings(
        settings,
        provider=str(recommended["provider"]),
        model=str(recommended["model"]),
        api_base=recommended.get("base_url"),
    )


def _guard_host_state(settings: Settings, args: argparse.Namespace) -> None:
    if getattr(args, "allow_host_state", False):
        return
    if Path("/.dockerenv").exists():
        return
    if settings.data_dir.is_absolute():
        return
    repo_root = _find_repo_root(Path.cwd())
    if repo_root is None:
        return
    if _command_safe_on_host(args):
        return
    raise RuntimeError(
        "host-state guard: this command would use the local .brigade state in the repository "
        "workspace. Use './ops/brigade-live.sh ...' for the running prototype, or rerun with "
        "'--allow-host-state' if you intentionally want host-local state."
    )


def _find_repo_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if (candidate / "docker-compose.yml").exists() and (
            candidate / "ops/brigade-live.sh"
        ).exists():
            return candidate
    return None


def _command_safe_on_host(args: argparse.Namespace) -> bool:
    if args.command in {"config", "db", "datastore"}:
        return True
    if args.command == "health":
        return True
    if args.command == "auth" and args.auth_command == "verify":
        return True
    if args.command == "model" and args.model_command == "complete":
        return True
    return False


def _status_payload(store: StateStore) -> dict[str, object]:
    assignments = [item.to_dict() for item in store.assignments()]
    return {
        "mission": store.mission().to_dict() if store.mission() else None,
        "users": [item.to_dict() for item in store.users()],
        "agents": [item.to_dict() for item in store.agents()],
        "teams": [item.to_dict() for item in store.teams()],
        "agent_states": {key: value.to_dict() for key, value in store.agent_states().items()},
        "goals": {
            key: [goal.to_dict() for goal in values]
            for key, values in store.goals().items()
        },
        "assignments": assignments,
        "assignment_history": store.assignment_history(),
        "alerts": store.alerts(),
        "knowledge_documents": store.knowledge_documents(),
        "knowledge_chunks": store.knowledge_chunks(),
        "episodes": store.episodes(),
        "provenance_records": store.provenance_records(),
        "messages": [item.to_dict() for item in store.messages()],
        "orchestrator_reasoning": store.orchestrator_reasoning(),
        "usage_records": store.usage_records(),
        "transcripts": store.transcripts(),
        "cloud_jobs": store.cloud_jobs(),
        "local_inference": store.local_inference(),
        "financial_report": store.latest_financial_report(),
    }


def _format_status(payload: dict[str, object]) -> str:
    assignments = payload.get("assignments", [])
    if not assignments:
        return "No assignments."
    return "\n".join(
        f"{item['assignment_id']} {item['status']} {item['assigned_to']}: {item['assignment']}"
        for item in assignments
    )


def _format_health(checks: list[object]) -> str:
    return "\n".join(
        f"{item.name}: {'ok' if item.ok else 'failed'} - {item.detail}"  # type: ignore[attr-defined]
        for item in checks
    )


def _format_dashboard_summary(store: StateStore) -> str:
    mission = store.mission()
    financial_report = store.latest_financial_report()
    lines = ["OpenBrigade Dashboard", ""]
    lines.append(f"Mission: {mission.statement if mission else 'not set'}")
    lines.append("")
    lines.append("Agents:")
    states = store.agent_states()
    for agent in store.agents():
        state = states.get(agent.agent_id)
        if state is None:
            summary = "idle"
        elif state.current_assignment_summary:
            summary = f"{state.status}: {state.current_assignment_summary}"
        else:
            summary = state.status
        lines.append(f"- {agent.agent_id} ({agent.role}): {summary}")
    lines.append("")
    lines.append(f"Active assignments: {len(store.assignments())}")
    lines.append(f"Archived assignments: {len(store.assignment_history())}")
    lines.append(f"Alerts: {len(store.alerts())}")
    if financial_report:
        lines.append(
            "Financial report: "
            f"cost=${financial_report['total_estimated_cost_usd']:.6f} "
            f"cloud_in_flight={financial_report['cloud_jobs_in_flight']}"
        )
    return "\n".join(lines)


def _find_agent(store: StateStore, agent_id: str) -> Agent | None:
    return next((item for item in store.agents() if item.agent_id == agent_id), None)


def _require_known_agent(store: StateStore, agent_id: str) -> None:
    if _find_agent(store, agent_id) is None:
        raise ValueError(f"unknown agent: {agent_id}")


def _find_team(store: StateStore, team_id: str | None) -> Team | None:
    if team_id is None:
        return None
    return next((item for item in store.teams() if item.team_id == team_id), None)


def _agent_with_team(agent: Agent, team_id: str) -> Agent:
    return Agent(
        agent_id=agent.agent_id,
        display_name=agent.display_name,
        workspace_path=agent.workspace_path,
        role=agent.role,
        team_id=team_id,
        model_provider=agent.model_provider,
        model_name=agent.model_name,
        created_at=agent.created_at,
    )


def _team_with_member(team: Team, agent_id: str, *, crew_chief: bool = False) -> Team:
    members = list(dict.fromkeys([*team.members, agent_id]))
    return Team(
        team_id=team.team_id,
        display_name=team.display_name,
        description=team.description,
        parent_team_id=team.parent_team_id,
        crew_chief_id=agent_id if crew_chief else team.crew_chief_id,
        members=members,
        delegation_policy=team.delegation_policy,
        escalation_team_id=team.escalation_team_id,
        created_at=team.created_at,
    )


def _team_with_policy(
    team: Team,
    *,
    delegation_policy: str,
    escalation_team_id: str | None,
) -> Team:
    return Team(
        team_id=team.team_id,
        display_name=team.display_name,
        description=team.description,
        parent_team_id=team.parent_team_id,
        crew_chief_id=team.crew_chief_id,
        members=team.members,
        delegation_policy=delegation_policy,
        escalation_team_id=escalation_team_id,
        created_at=team.created_at,
    )


def _team_view(team: Team, store: StateStore) -> dict[str, object]:
    agents = {agent.agent_id: agent for agent in store.agents()}
    return {
        **team.to_dict(),
        "member_agents": [
            agents[agent_id].to_dict() for agent_id in team.members if agent_id in agents
        ],
        "crew_chief": agents[team.crew_chief_id].to_dict()
        if team.crew_chief_id in agents
        else None,
    }


def _team_status_view(store: StateStore, team_id: str) -> dict[str, Any]:
    team = _find_team(store, team_id)
    if team is None:
        raise ValueError(f"unknown team: {team_id}")
    child_team_ids = _team_descendant_ids(store.teams(), team_id)
    scoped_team_ids = {team_id, *child_team_ids}
    agents = [
        agent
        for agent in store.agents()
        if agent.team_id in scoped_team_ids or agent.agent_id in team.members
    ]
    agent_ids = {agent.agent_id for agent in agents}
    assignments = [
        assignment.to_dict()
        for assignment in store.assignments()
        if assignment.assigned_to in agent_ids
    ]
    goals = {
        agent_id: [goal.to_dict() for goal in values]
        for agent_id, values in store.goals().items()
        if agent_id in agent_ids
    }
    states = store.agent_states()
    blockers = [
        {
            "agent_id": assignment["assigned_to"],
            "assignment_id": assignment["assignment_id"],
            "blockers": assignment["blockers"],
            "status": assignment["status"],
        }
        for assignment in assignments
        if assignment["blockers"] or assignment["status"] == AssignmentStatus.BLOCKED.value
    ]
    return {
        "team": _team_view(team, store),
        "descendant_team_ids": child_team_ids,
        "agents": [
            {
                **agent.to_dict(),
                "state": states.get(agent.agent_id).to_dict()
                if states.get(agent.agent_id)
                else None,
            }
            for agent in agents
        ],
        "goals": goals,
        "active_assignments": assignments,
        "blockers": blockers,
    }


def _organization_graph(store: StateStore) -> dict[str, Any]:
    teams = store.teams()
    agents = store.agents()
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for team in teams:
        nodes.append(
            {
                "id": f"team:{team.team_id}",
                "kind": "team",
                "team_id": team.team_id,
                "display_name": team.display_name,
                "delegation_policy": team.delegation_policy,
                "escalation_team_id": team.escalation_team_id,
            }
        )
        if team.parent_team_id:
            edges.append(
                {
                    "from": f"team:{team.parent_team_id}",
                    "to": f"team:{team.team_id}",
                    "kind": "parent_of",
                }
            )
        if team.escalation_team_id:
            edges.append(
                {
                    "from": f"team:{team.team_id}",
                    "to": f"team:{team.escalation_team_id}",
                    "kind": "escalates_to",
                }
            )
        for member in team.members:
            edges.append(
                {
                    "from": f"agent:{member}",
                    "to": f"team:{team.team_id}",
                    "kind": "member_of",
                }
            )
        if team.crew_chief_id:
            edges.append(
                {
                    "from": f"agent:{team.crew_chief_id}",
                    "to": f"team:{team.team_id}",
                    "kind": "chief_of",
                }
            )
    for agent in agents:
        nodes.append(
            {
                "id": f"agent:{agent.agent_id}",
                "kind": "agent",
                "agent_id": agent.agent_id,
                "display_name": agent.display_name,
                "role": agent.role,
                "team_id": agent.team_id,
            }
        )
    return {
        "graph_id": str(uuid4()),
        "generated_at": utc_now_iso(),
        "nodes": nodes,
        "edges": edges,
        "team_count": len(teams),
        "agent_count": len(agents),
    }


def _persist_org_graph(
    store: StateStore,
    *,
    graph: dict[str, Any] | None = None,
    reason: str,
) -> dict[str, Any]:
    graph = graph or _organization_graph(store)
    record = {
        "record_id": str(uuid4()),
        "node_type": "organization_graph",
        "node_id": graph["graph_id"],
        "source_refs": [],
        "metadata": {"reason": reason},
        "graph": graph,
        "created_at": utc_now_iso(),
    }
    store.add_provenance_record(record)
    return record


def _validate_agent_assignment_authority(
    store: StateStore,
    *,
    creator_agent_id: str,
    target_agent_id: str,
    source: str,
) -> None:
    if source != "agent_delegate":
        return
    creator = _find_agent(store, creator_agent_id)
    if creator is None:
        raise PermissionError(f"{creator_agent_id} is not a registered agent")
    if _agent_can_direct(store, creator_agent_id, target_agent_id):
        return
    raise PermissionError(f"{creator_agent_id} cannot direct {target_agent_id}")


def _agent_can_direct(store: StateStore, creator_agent_id: str, target_agent_id: str) -> bool:
    if creator_agent_id == target_agent_id:
        return True
    teams = store.teams()
    target_team = _team_for_agent(store, target_agent_id)
    creator_team = _team_for_agent(store, creator_agent_id)
    if target_team is None:
        return False
    if target_team.delegation_policy == "orchestrator_only":
        return False
    if _chief_authorized_for_agent(teams, creator_agent_id, target_agent_id):
        return True
    return (
        target_team.delegation_policy == "open"
        and creator_team is not None
        and creator_team.team_id == target_team.team_id
    )


def _chief_authorized_for_agent(
    teams: list[Team],
    chief_agent_id: str,
    target_agent_id: str,
) -> bool:
    chief_teams = [team for team in teams if team.crew_chief_id == chief_agent_id]
    if not chief_teams:
        return False
    for team in chief_teams:
        scoped_team_ids = {team.team_id, *_team_descendant_ids(teams, team.team_id)}
        for candidate in teams:
            if candidate.team_id in scoped_team_ids and target_agent_id in candidate.members:
                return True
    return False


def _team_for_agent(store: StateStore, agent_id: str) -> Team | None:
    agent = _find_agent(store, agent_id)
    teams = store.teams()
    if agent and agent.team_id:
        team = next((item for item in teams if item.team_id == agent.team_id), None)
        if team is not None:
            return team
    return next((team for team in teams if agent_id in team.members), None)


def _team_descendant_ids(teams: list[Team], team_id: str) -> list[str]:
    children = [team.team_id for team in teams if team.parent_team_id == team_id]
    descendants: list[str] = []
    for child in children:
        descendants.append(child)
        descendants.extend(_team_descendant_ids(teams, child))
    return descendants


def _delegate_from_crew_chief(
    store: StateStore,
    *,
    team_id: str,
    chief_agent_id: str,
    target_agent_id: str,
    assignment_text: str,
    goal_statement: str | None,
    rationale: str | None,
    priority: Priority,
    current_user: User | None,
) -> dict[str, Any]:
    # Delegation lives in brigade.services so the CLI and web gateway share one
    # implementation; this thin wrapper preserves the existing CLI call site.
    return delegate_from_crew_chief(
        store,
        team_id=team_id,
        chief_agent_id=chief_agent_id,
        target_agent_id=target_agent_id,
        assignment_text=assignment_text,
        goal_statement=goal_statement,
        rationale=rationale,
        priority=priority,
        current_user=current_user,
    )


def _route_team_work(
    store: StateStore,
    *,
    team_id: str,
    assignment_text: str,
    scope: str,
    urgency: str,
    goal_statement: str | None,
    current_user: User | None,
) -> dict[str, Any]:
    team = _find_team(store, team_id)
    if team is None:
        raise ValueError(f"unknown team: {team_id}")
    if not team.members:
        raise ValueError(f"team {team_id} has no members")
    agents = {agent.agent_id: agent for agent in store.agents()}
    active_agents = {assignment.assigned_to for assignment in store.assignments()}
    member_ids = [member for member in team.members if member in agents]
    if not member_ids:
        raise ValueError(f"team {team_id} has no registered members")

    route_to_chief = (
        scope == "team"
        or urgency in {"high", "urgent"}
        or team.delegation_policy in {"chief_only", "orchestrator_only"}
    )
    assignee = team.crew_chief_id if route_to_chief and team.crew_chief_id else None
    if assignee is None:
        assignee = next(
            (
                member
                for member in member_ids
                if member != team.crew_chief_id and member not in active_agents
            ),
            member_ids[0],
        )
    if assignee not in member_ids:
        raise ValueError(f"team {team_id} chief {assignee} is not a registered member")

    priority = Priority.URGENT if urgency == "urgent" else Priority(urgency)
    rationale = (
        f"Team-aware route: scope={scope}, urgency={urgency}, "
        f"policy={team.delegation_policy}."
    )
    assignment = Assignment(
        assignment=assignment_text,
        assigned_to=assignee,
        created_by="orchestrator",
        source="team_route",
        priority=priority,
        work_mode=WorkMode.HEARTBEAT,
        goal_statement=goal_statement,
        assignment_rationale=rationale,
        created_by_user_id=current_user.username if current_user else None,
        created_by_role="team_router",
        idempotency_key=f"team-route:{team_id}:{scope}:{urgency}:{assignment_text}",
    )
    persisted = store.add_assignment(assignment)
    created = persisted.assignment_id == assignment.assignment_id
    decision = {
        "team_id": team_id,
        "assignee": assignee,
        "scope": scope,
        "urgency": urgency,
        "delegation_policy": team.delegation_policy,
        "rationale": rationale,
        "assignment_id": persisted.assignment_id,
    }
    if created:
        store.add_orchestrator_reasoning(
            _reasoning_event(
                source="team_route",
                decision_summary=f"routed team {team_id} work to {assignee}",
                payload=decision,
            )
        )
    return {
        "status": "queued" if created else "existing",
        "decision": decision,
        "assignment": persisted.to_dict(),
    }


def _escalate_team_work(
    store: StateStore,
    *,
    from_team_id: str,
    to_team_id: str,
    chief_agent_id: str,
    assignment_text: str,
    reason: str,
    current_user: User | None,
) -> dict[str, Any]:
    from_team = _find_team(store, from_team_id)
    to_team = _find_team(store, to_team_id)
    if from_team is None:
        raise ValueError(f"unknown source team: {from_team_id}")
    if to_team is None:
        raise ValueError(f"unknown destination team: {to_team_id}")
    if from_team.crew_chief_id != chief_agent_id:
        raise PermissionError(f"{chief_agent_id} is not Crew Chief for team {from_team_id}")
    if to_team.crew_chief_id is None:
        raise ValueError(f"destination team {to_team_id} has no Crew Chief")

    assignment = Assignment(
        assignment=assignment_text,
        assigned_to=to_team.crew_chief_id,
        created_by=chief_agent_id,
        source="cross_team_escalation",
        priority=Priority.HIGH,
        work_mode=WorkMode.HEARTBEAT,
        assignment_rationale=reason,
        created_by_user_id=current_user.username if current_user else None,
        created_by_role="crew_chief",
        idempotency_key=f"cross-team:{from_team_id}:{to_team_id}:{assignment_text}",
    )
    persisted = store.add_assignment(assignment)
    created = persisted.assignment_id == assignment.assignment_id
    if created:
        message = ChatMessage(
            channel=f"team-escalation:{from_team_id}:{to_team_id}:{persisted.assignment_id}",
            sender=chief_agent_id,
            recipient=to_team.crew_chief_id,
            content=assignment_text,
            metadata={
                "kind": "cross_team_escalation",
                "from_team": from_team_id,
                "to_team": to_team_id,
                "reason": reason,
                "assignment_id": persisted.assignment_id,
            },
        )
        store.add_message(message)
    payload = {
        "from_team": from_team_id,
        "to_team": to_team_id,
        "requesting_chief": chief_agent_id,
        "receiving_chief": to_team.crew_chief_id,
        "assignment_id": persisted.assignment_id,
        "message_id": message.message_id if created else None,
        "reason": reason,
    }
    if created:
        store.add_orchestrator_reasoning(
            _reasoning_event(
                source="cross_team_escalation",
                decision_summary=(
                    f"{from_team_id} escalated work to {to_team_id} "
                    f"via {to_team.crew_chief_id}"
                ),
                payload=payload,
            )
        )
    return {
        "status": "queued" if created else "existing",
        "escalation": payload,
        "assignment": persisted.to_dict(),
    }


def _propose_stalled_goal_work(
    store: StateStore,
    *,
    agent_id: str | None = None,
) -> dict[str, Any]:
    known_agents = {agent.agent_id for agent in store.agents()}
    goals_by_agent = store.goals(agent_id)
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    active_assignments = store.assignments()
    existing_keys = {
        assignment.idempotency_key
        for assignment in active_assignments
        if assignment.idempotency_key is not None
    }
    for goal_agent_id, goals in goals_by_agent.items():
        if goal_agent_id not in known_agents:
            skipped.append({"agent_id": goal_agent_id, "reason": "unknown agent"})
            continue
        for goal in goals:
            key = f"goal-stall:{goal_agent_id}:{goal.statement}"
            if key in existing_keys:
                skipped.append({"agent_id": goal_agent_id, "reason": "already queued"})
                continue
            has_active_goal_work = any(
                assignment.assigned_to == goal_agent_id
                and assignment.goal_statement == goal.statement
                for assignment in active_assignments
            )
            if has_active_goal_work:
                skipped.append({"agent_id": goal_agent_id, "reason": "active goal work exists"})
                continue
            assignment = Assignment(
                assignment=f"Advance goal: {goal.statement}",
                assigned_to=goal_agent_id,
                created_by="orchestrator",
                source="goal_stall_detector",
                priority=Priority.NORMAL,
                work_mode=WorkMode.HEARTBEAT,
                goal_statement=goal.statement,
                assignment_rationale="No active assignment is advancing this goal.",
                idempotency_key=key,
            )
            persisted = store.add_assignment(assignment)
            if persisted.assignment_id != assignment.assignment_id:
                skipped.append({"agent_id": goal_agent_id, "reason": "already handled"})
                continue
            active_assignments.append(persisted)
            existing_keys.add(key)
            created.append(persisted.to_dict())

    mission = store.mission()
    events = [
        orchestration_event(
            "created_work",
            f"Created stalled-goal assignment {item['assignment_id']} for {item['assigned_to']}.",
            source="goal_stall_detector",
            decision="created",
            status="created",
            mission_statement=mission.statement if mission else None,
            goal_statement=item.get("goal_statement"),
            trigger="stalled_goal_without_active_work",
            assignment_id=item["assignment_id"],
            assignment_ids=[item["assignment_id"]],
            agent_id=item["assigned_to"],
            idempotency_key=item.get("idempotency_key"),
            payload=item,
        )
        for item in created
    ]
    events.extend(
        orchestration_event(
            "proactive_skip",
            f"Skipped stalled-goal work for {item.get('agent_id')}: {item.get('reason')}.",
            source="goal_stall_detector",
            decision="skipped",
            status="skipped",
            mission_statement=mission.statement if mission else None,
            trigger=str(item.get("reason") or "skipped"),
            agent_id=item.get("agent_id"),
            payload=item,
        )
        for item in skipped
    )
    record = _reasoning_event(
        source="goal_stall_detector",
        decision_summary=(
            f"created={len(created)} skipped={len(skipped)} stalled-goal assignments"
        ),
        payload={"created": created, "skipped": skipped},
        events=events,
        mission_statement=mission.statement if mission else None,
    )
    store.add_orchestrator_reasoning(record)
    return {"created": created, "skipped": skipped}


def _dispatch_cloud_job(
    store: StateStore,
    *,
    agent_id: str,
    assignment_text: str,
    provider: str,
    model: str,
    max_cost_usd: float | None,
    current_user: User | None,
) -> dict[str, Any]:
    agent = _find_agent(store, agent_id)
    if agent is None:
        raise ValueError(f"unknown agent: {agent_id}")
    active_cloud_jobs = [
        job for job in store.cloud_jobs() if job.get("status") not in {"complete", "failed"}
    ]
    if active_cloud_jobs:
        raise RuntimeError("cloud dispatch blocked while another cloud job is already in flight")

    assignment = Assignment(
        assignment=assignment_text,
        assigned_to=agent_id,
        created_by=current_user.username if current_user else "human",
        source="cloud_dispatch",
        priority=Priority.HIGH,
        work_mode=WorkMode.EXTENDED,
        estimated_cycles=3,
        assignment_rationale="Queued for extended cloud-backed work.",
        created_by_user_id=current_user.username if current_user else None,
        created_by_role=current_user.role.value if current_user else None,
        idempotency_key=f"cloud-dispatch:{agent_id}:{assignment_text}",
    )
    persisted = store.add_assignment(assignment)
    created = persisted.assignment_id == assignment.assignment_id
    job = {
        "job_id": str(uuid4()),
        "assignment_id": persisted.assignment_id,
        "agent_id": agent_id,
        "provider": provider,
        "model": model,
        "status": "queued" if created else "existing",
        "max_cost_usd": max_cost_usd,
        "requested_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "source": "cloud_dispatch",
    }
    if created:
        store.upsert_cloud_job(job)
        store.add_orchestrator_reasoning(
            _reasoning_event(
                source="cloud_dispatch",
                decision_summary=f"queued cloud job {job['job_id']} for {agent_id}",
                payload={"job": job, "assignment": persisted.to_dict()},
            )
        )
    return {
        "status": "queued" if created else "existing",
        "job": job,
        "assignment": persisted.to_dict(),
    }


def _resolve_cloud_job(
    store: StateStore,
    *,
    job_id: str,
    status: str,
    summary: str,
) -> dict[str, Any]:
    job = next((item for item in store.cloud_jobs() if item.get("job_id") == job_id), None)
    if job is None:
        raise ValueError(f"unknown cloud job: {job_id}")
    job["status"] = status
    job["summary"] = summary
    job["updated_at"] = utc_now_iso()
    store.upsert_cloud_job(job)

    assignment_record: dict[str, Any] | None = None
    assignment_id = str(job.get("assignment_id") or "")
    assignment = store.find_assignment(assignment_id) if assignment_id else None
    if assignment is not None:
        assignment.progress_summary = summary
        assignment.updated_at = utc_now_iso()
        assignment.status = (
            AssignmentStatus.COMPLETE if status == "complete" else AssignmentStatus.FAILED
        )
        store.archive_assignment(assignment, executive_summary=summary)
        assignment_record = assignment.to_dict()

    store.add_orchestrator_reasoning(
        _reasoning_event(
            source="cloud_resolve",
            decision_summary=f"cloud job {job_id} marked {status}",
            payload={"job": job, "assignment": assignment_record},
        )
    )
    return {"status": status, "job": job, "assignment": assignment_record}


def _audit_alerts(
    store: StateStore,
    settings: Settings,
    *,
    failure_threshold: int,
    include_health: bool,
) -> dict[str, Any]:
    if failure_threshold < 1:
        raise ValueError("--failure-threshold must be at least 1")
    existing = set(store.alerts())
    created: list[str] = []
    findings: list[dict[str, Any]] = []

    def add_alert(message: str, finding: dict[str, Any]) -> None:
        findings.append(finding)
        if message not in existing:
            store.add_alert(message)
            existing.add(message)
            created.append(message)

    for assignment in store.assignments():
        if assignment.consecutive_failures >= failure_threshold:
            message = (
                f"assignment {assignment.assignment_id} has "
                f"{assignment.consecutive_failures} consecutive failures"
            )
            add_alert(
                message,
                {
                    "kind": "repeated_task_failure",
                    "assignment_id": assignment.assignment_id,
                    "agent_id": assignment.assigned_to,
                    "consecutive_failures": assignment.consecutive_failures,
                },
            )

    goals_by_agent = store.goals()
    active_assignments = store.assignments()
    for goal_agent_id, goals in goals_by_agent.items():
        for goal in goals:
            has_active_goal_work = any(
                assignment.assigned_to == goal_agent_id
                and assignment.goal_statement == goal.statement
                for assignment in active_assignments
            )
            if has_active_goal_work:
                continue
            message = f"goal drift: {goal_agent_id} has no active work for goal '{goal.statement}'"
            add_alert(
                message,
                {
                    "kind": "goal_drift",
                    "agent_id": goal_agent_id,
                    "goal_statement": goal.statement,
                },
            )

    for job in store.cloud_jobs("failed"):
        message = f"cloud job {job.get('job_id')} failed for {job.get('agent_id')}"
        add_alert(message, {"kind": "cloud_job_failed", "job": job})

    if include_health:
        if settings.jwt_secret == DEFAULT_JWT_SECRET or len(settings.jwt_secret) < 32:
            message = "security warning: JWT secret is default or shorter than 32 characters"
            add_alert(
                message,
                {
                    "kind": "weak_jwt_secret",
                    "require_auth": settings.require_auth,
                    "minimum_length": 32,
                },
            )

        if not settings.require_auth and settings.web_host not in {"127.0.0.1", "localhost", "::1"}:
            message = (
                "security warning: web gateway is configured for a reachable host "
                "with auth disabled"
            )
            add_alert(
                message,
                {
                    "kind": "web_auth_disabled_reachable_host",
                    "web_host": settings.web_host,
                },
            )

        for check in check_configured_datastores(settings):
            if check.ok:
                continue
            message = f"datastore failure: {check.name}: {check.detail}"
            add_alert(
                message,
                {"kind": "datastore_failure", "name": check.name, "detail": check.detail},
            )

    store.add_orchestrator_reasoning(
        _reasoning_event(
            source="alert_audit",
            decision_summary=f"created={len(created)} findings={len(findings)}",
            payload={"created": created, "findings": findings, "include_health": include_health},
        )
    )
    return {"alerts_created": created, "findings": findings}


def _ask_agent_chat(
    store: StateStore,
    actor: AuthResult,
    *,
    from_agent_id: str,
    to_agent_id: str,
    content: str,
    provider: Any,
    channel: str | None = None,
) -> dict[str, Any]:
    from_agent = _find_agent(store, from_agent_id)
    if from_agent is None:
        raise ValueError(f"unknown from-agent: {from_agent_id}")
    to_agent = _find_agent(store, to_agent_id)
    if to_agent is None:
        raise ValueError(f"unknown to-agent: {to_agent_id}")

    conversation_id = channel or f"agent:{from_agent_id}:{to_agent_id}:{uuid4()}"
    metadata = _chat_metadata(store, actor, from_agent_id)
    request = ChatMessage(
        channel=conversation_id,
        sender=from_agent_id,
        recipient=to_agent_id,
        content=content,
        metadata={
            **metadata,
            "kind": "agent_chat_request",
            "conversation_id": conversation_id,
            "from_agent": from_agent.to_dict(),
            "to_agent": to_agent.to_dict(),
        },
    )
    store.add_message(request)

    route_type = getattr(provider, "route_type", "unknown")
    lock_acquired = False
    try:
        if route_type == "local":
            _acquire_local_inference_lock(store, to_agent_id)
            lock_acquired = True
        response = provider.complete(_agent_chat_prompt(from_agent, to_agent, content))
    except RuntimeError as exc:
        summary = str(exc)
        store.add_alert(f"agent chat {conversation_id}: {summary}")
        return {
            "conversation_id": conversation_id,
            "status": "blocked",
            "summary": summary,
            "request_message_id": request.message_id,
            "response_message_id": None,
            "from_agent": from_agent_id,
            "to_agent": to_agent_id,
            "route_type": route_type,
            "task_links": [],
        }
    finally:
        if lock_acquired:
            _release_local_inference_lock(store, to_agent_id)

    response_text = response.text.strip()
    response_message = ChatMessage(
        channel=conversation_id,
        sender=to_agent_id,
        recipient=from_agent_id,
        content=response_text,
        metadata={
            "kind": "agent_chat_response",
            "conversation_id": conversation_id,
            "provider": response.provider,
            "model": response.model,
            "route_type": response.route_type,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "estimated_cost_usd": response.estimated_cost_usd,
        },
    )
    store.add_message(response_message)
    store.add_usage_record(
        {
            "usage_id": str(uuid4()),
            "assignment_id": None,
            "agent_id": to_agent_id,
            "provider": response.provider,
            "model": response.model,
            "route_type": response.route_type,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "total_tokens": response.input_tokens + response.output_tokens,
            "estimated_cost_usd": response.estimated_cost_usd,
            "recorded_at": utc_now_iso(),
            "conversation_id": conversation_id,
            "source": "agent_chat",
        }
    )
    _record_agent_chat_episodes(
        store,
        conversation_id=conversation_id,
        from_agent_id=from_agent_id,
        to_agent_id=to_agent_id,
        request=content,
        response=response_text,
    )
    summary = _summarize_chat_response(response_text)
    return {
        "conversation_id": conversation_id,
        "status": "complete",
        "summary": summary,
        "request_message_id": request.message_id,
        "response_message_id": response_message.message_id,
        "from_agent": from_agent_id,
        "to_agent": to_agent_id,
        "provider": response.provider,
        "model": response.model,
        "route_type": response.route_type,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "task_links": [],
    }


def _group_chat(
    store: StateStore,
    actor: AuthResult,
    *,
    participants: list[str],
    agenda: str,
    provider: Any,
    moderator: str,
    max_turns: int,
    channel: str | None = None,
) -> dict[str, Any]:
    if max_turns < 1:
        raise ValueError("--max-turns must be at least 1")
    participant_ids = list(dict.fromkeys(participants))
    if len(participant_ids) < 2:
        raise ValueError("group chat requires at least two unique participants")
    agents: dict[str, Agent] = {}
    for agent_id in participant_ids:
        agent = _find_agent(store, agent_id)
        if agent is None:
            raise ValueError(f"unknown participant: {agent_id}")
        agents[agent_id] = agent

    conversation_id = channel or f"group:{uuid4()}"
    created_at = utc_now_iso()
    kickoff = ChatMessage(
        channel=conversation_id,
        sender=moderator,
        recipient=",".join(participant_ids),
        content=agenda,
        metadata={
            **_chat_metadata(store, actor, moderator),
            "kind": "group_chat_start",
            "conversation_id": conversation_id,
            "participants": participant_ids,
            "current_speaker": participant_ids[0],
            "max_turns": max_turns,
        },
    )
    store.add_message(kickoff)

    turns: list[dict[str, Any]] = []
    route_type = getattr(provider, "route_type", "unknown")
    lock_acquired = False
    try:
        if route_type == "local":
            _acquire_local_inference_lock(store, "group_chat")
            lock_acquired = True
        previous_turns: list[str] = []
        for turn_index in range(max_turns):
            speaker_id = participant_ids[turn_index % len(participant_ids)]
            next_speaker_id = participant_ids[(turn_index + 1) % len(participant_ids)]
            speaker = agents[speaker_id]
            response = provider.complete(
                _group_chat_prompt(
                    speaker,
                    agenda=agenda,
                    participants=participant_ids,
                    turn_index=turn_index + 1,
                    max_turns=max_turns,
                    previous_turns=previous_turns,
                    next_speaker_id=next_speaker_id,
                )
            )
            response_text = response.text.strip()
            message = ChatMessage(
                channel=conversation_id,
                sender=speaker_id,
                recipient=next_speaker_id,
                content=response_text,
                metadata={
                    "kind": "group_chat_turn",
                    "conversation_id": conversation_id,
                    "turn_index": turn_index + 1,
                    "speaker": speaker_id,
                    "next_speaker": next_speaker_id,
                    "provider": response.provider,
                    "model": response.model,
                    "route_type": response.route_type,
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "estimated_cost_usd": response.estimated_cost_usd,
                },
            )
            store.add_message(message)
            store.add_usage_record(
                {
                    "usage_id": str(uuid4()),
                    "assignment_id": None,
                    "agent_id": speaker_id,
                    "provider": response.provider,
                    "model": response.model,
                    "route_type": response.route_type,
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "total_tokens": response.input_tokens + response.output_tokens,
                    "estimated_cost_usd": response.estimated_cost_usd,
                    "recorded_at": utc_now_iso(),
                    "conversation_id": conversation_id,
                    "source": "group_chat",
                    "turn_index": turn_index + 1,
                }
            )
            turn = {
                "turn_index": turn_index + 1,
                "speaker": speaker_id,
                "next_speaker": next_speaker_id,
                "message_id": message.message_id,
                "summary": _summarize_chat_response(response_text),
            }
            turns.append(turn)
            previous_turns.append(f"{speaker_id}: {response_text}")
    except RuntimeError as exc:
        summary = str(exc)
        store.add_alert(f"group chat {conversation_id}: {summary}")
        return {
            "conversation_id": conversation_id,
            "status": "blocked",
            "summary": summary,
            "participants": participant_ids,
            "turns": turns,
            "kickoff_message_id": kickoff.message_id,
            "task_links": [],
        }
    finally:
        if lock_acquired:
            _release_local_inference_lock(store, "group_chat")

    summary = _summarize_chat_response(" ".join(turn["summary"] for turn in turns))
    _record_group_chat_episodes(
        store,
        conversation_id=conversation_id,
        participants=participant_ids,
        agenda=agenda,
        summary=summary,
        turns=turns,
        created_at=created_at,
    )
    return {
        "conversation_id": conversation_id,
        "status": "complete",
        "summary": summary,
        "participants": participant_ids,
        "turns": turns,
        "kickoff_message_id": kickoff.message_id,
        "task_links": [],
    }


def _agent_chat_prompt(from_agent: Agent, to_agent: Agent, content: str) -> str:
    return "\n".join(
        [
            f"You are {to_agent.display_name} ({to_agent.agent_id}), role: {to_agent.role}.",
            f"{from_agent.display_name} ({from_agent.agent_id}) is asking you a direct question.",
            "Answer the asking agent directly. Keep the response useful and concise.",
            "",
            "Question:",
            content,
        ]
    )


def _group_chat_prompt(
    speaker: Agent,
    *,
    agenda: str,
    participants: list[str],
    turn_index: int,
    max_turns: int,
    previous_turns: list[str],
    next_speaker_id: str,
) -> str:
    prior = "\n".join(previous_turns[-4:]) if previous_turns else "No prior turns."
    return "\n".join(
        [
            f"You are {speaker.display_name} ({speaker.agent_id}), role: {speaker.role}.",
            "You are in a serialized group chat. Only you may speak on this turn.",
            f"Participants: {', '.join(participants)}",
            f"Turn: {turn_index} of {max_turns}",
            f"After your response, pass the mic to {next_speaker_id}.",
            "",
            "Agenda:",
            agenda,
            "",
            "Prior turns:",
            prior,
            "",
            "Respond concisely with your contribution and one sentence explaining why "
            f"{next_speaker_id} should speak next.",
        ]
    )


def _record_agent_chat_episodes(
    store: StateStore,
    *,
    conversation_id: str,
    from_agent_id: str,
    to_agent_id: str,
    request: str,
    response: str,
) -> None:
    created_at = utc_now_iso()
    summary = _summarize_chat_response(response)
    for agent_id in (from_agent_id, to_agent_id):
        store.add_episode(
            {
                "episode_id": str(uuid4()),
                "agent_id": agent_id,
                "created_at": created_at,
                "source": "agent_chat",
                "conversation_id": conversation_id,
                "participants": [from_agent_id, to_agent_id],
                "summary": summary,
                "request": request,
                "response": response,
            }
        )


def _record_group_chat_episodes(
    store: StateStore,
    *,
    conversation_id: str,
    participants: list[str],
    agenda: str,
    summary: str,
    turns: list[dict[str, Any]],
    created_at: str,
) -> None:
    for agent_id in participants:
        store.add_episode(
            {
                "episode_id": str(uuid4()),
                "agent_id": agent_id,
                "created_at": created_at,
                "source": "group_chat",
                "conversation_id": conversation_id,
                "participants": participants,
                "agenda": agenda,
                "summary": summary,
                "turns": turns,
            }
        )


def _summarize_chat_response(response: str) -> str:
    stripped = " ".join(response.split())
    if len(stripped) <= 240:
        return stripped
    return stripped[:237].rstrip() + "..."


def _reasoning_event(
    *,
    source: str,
    decision_summary: object,
    payload: dict[str, Any],
    events: list[dict[str, Any]] | None = None,
    mission_statement: str | None = None,
) -> dict[str, Any]:
    now = utc_now_iso()
    return {
        "reasoning_id": str(uuid4()),
        "cycle_id": str(uuid4()),
        "started_at": now,
        "ended_at": now,
        "source": source,
        "mission_statement": mission_statement,
        "queued_assignments": [],
        "assigned": [],
        "skipped": [],
        "alerts": [],
        "agent_states": {},
        "decision_summary": str(decision_summary),
        "events": list(events or []),
        "payload": payload,
    }


def _bootstrap_mvp(
    store: StateStore,
    data_dir: Path,
    mission_statement: str,
    *,
    force: bool = False,
) -> None:
    existing_goals = any(store.goals().values())
    already_initialized = bool(store.mission() or store.users() or store.agents() or existing_goals)
    if already_initialized and not force:
        raise RuntimeError("prototype already initialized; rerun with --force to reseed defaults")
    store.set_mission(
        Mission(
            statement=mission_statement,
            success_criteria=["Monthly value or revenue exceeds operating cost."],
            explicitly_not=["spam users", "make unsupported financial claims"],
        )
    )
    defaults = [
        Agent("sage", "SAGE", "workspace-sage", "crew_chief"),
        Agent("garde", "GARDE", "workspace-garde", "infrastructure"),
        Agent("abacus", "ABACUS", "workspace-abacus", "financial"),
    ]
    known_agents = {item.agent_id for item in store.agents()}
    for agent in defaults:
        if agent.agent_id not in known_agents:
            store.add_agent(agent)
        ensure_agent_workspace(agent, data_dir)
    if _find_user(store, "owner") is None:
        store.add_user(User("owner", Role.OWNER))
    store.ensure_goal(
        "sage",
        Goal(
            statement="Turn the mission into useful plans and written artifacts.",
            success_criteria=["approved plan or artifact exists"],
            explicitly_not=["ignore operator direction"],
            set_by="human",
            human_confirmed=True,
        ),
    )
    store.ensure_goal(
        "garde",
        Goal(
            statement="Keep the harness running reliably.",
            success_criteria=["health checks and task flow remain green"],
            explicitly_not=["touch production containers"],
            set_by="human",
            human_confirmed=True,
        ),
    )
    store.ensure_goal(
        "abacus",
        Goal(
            statement="Track cost and identify sustainable revenue experiments.",
            success_criteria=["cost report or revenue experiment exists"],
            explicitly_not=["make unsupported financial claims"],
            set_by="human",
            human_confirmed=True,
        ),
    )
    store.dedupe_goals()


def _run_cycle(
    store: StateStore,
    provider: ModelProvider | None = None,
    *,
    stale_work_seconds: int = 86_400,
    proactive_config: ProactiveContinuationConfig | None = None,
    orchestration_config: OrchestrationConfig | None = None,
) -> FullCycleResult:
    config = orchestration_config
    if config is None:
        proactive = proactive_config or ProactiveContinuationConfig()
        config = OrchestrationConfig(
            stale_work_seconds=stale_work_seconds,
            proactive_mode=proactive.mode,
            proactive_creation_enabled=proactive.creation_enabled,
            max_proactive_proposals_per_cycle=proactive.max_proposals_per_cycle,
            max_proactive_creations_per_cycle=proactive.max_creations_per_cycle,
        )
    return run_full_cycle(store, provider=provider, config=config)


def _proactive_config_from_settings(settings: Settings) -> ProactiveContinuationConfig:
    return ProactiveContinuationConfig(
        mode=settings.proactive_mode,
        creation_enabled=settings.proactive_creation_enabled,
        max_proposals_per_cycle=settings.max_proactive_proposals_per_cycle,
        max_creations_per_cycle=settings.max_proactive_creations_per_cycle,
    )


def _workspace_for_agent(store: StateStore, data_dir: Path, agent_id: str) -> Path:
    agent = next((item for item in store.agents() if item.agent_id == agent_id), None)
    if agent is None:
        raise ValueError(f"unknown agent: {agent_id}")
    return ensure_agent_workspace(agent, data_dir)


def _ingest_document(
    store: StateStore,
    title: str,
    source: str,
    document_type: str,
    path: str,
) -> dict[str, object]:
    document, chunks, episode, provenance = ingest_local_document(
        title=title,
        source=source,
        document_type=document_type,
        content_path=path,
    )
    store.add_knowledge_document(document.to_dict())
    for chunk in chunks:
        store.add_knowledge_chunk(chunk)
    store.add_episode(episode)
    for record in provenance:
        store.add_provenance_record(record)
    return document.to_dict()


def _resolve_actor(
    store: StateStore,
    settings: Settings,
    args: argparse.Namespace,
) -> AuthResult:
    users = store.users()
    token = getattr(args, "token", None)
    acting_user = getattr(args, "as_user", None)
    if token:
        result = verify_token(settings, token)
        if not result.ok:
            return result
        stored_user = _find_user(store, result.user.username) if result.user else None
        return AuthResult(
            ok=True,
            method="jwt",
            user=stored_user or result.user,
            claims=result.claims,
        )
    if acting_user:
        user = _find_user(store, acting_user)
        if user is None:
            return AuthResult(ok=False, method="as-user", reason=f"unknown user: {acting_user}")
        return AuthResult(ok=True, method="as-user", user=user)
    if not users:
        return AuthResult(ok=True, method="bootstrap", user=None)
    if len(users) == 1 and not settings.require_auth:
        return AuthResult(ok=True, method="implicit-single-user", user=users[0])
    owner_users = [item for item in users if item.role == Role.OWNER]
    if len(owner_users) == 1 and not settings.require_auth:
        return AuthResult(ok=True, method="implicit-owner", user=owner_users[0])
    return AuthResult(ok=False, method="none", reason="no authenticated actor")


def _require_permission(
    store: StateStore,
    settings: Settings,
    actor: AuthResult,
    permission: str,
    *,
    allow_bootstrap: bool = False,
    allow_unauth: bool = False,
) -> User | None:
    if allow_unauth and not settings.require_auth:
        return actor.user
    if not store.users() and not settings.require_auth:
        return actor.user
    if allow_bootstrap and not store.users():
        return actor.user
    if not actor.ok:
        raise PermissionError(actor.reason or "authentication failed")
    if actor.user is None:
        raise PermissionError("no authenticated actor")
    if not can(actor.user, permission):
        raise PermissionError(f"{actor.user.username} lacks permission: {permission}")
    return actor.user


def _find_user(store: StateStore, username: str) -> User | None:
    return next((item for item in store.users() if item.username == username), None)


def _interactive_task_prompt(store: StateStore, user: User | None) -> dict[str, Any]:
    agents = [item.agent_id for item in store.agents()]
    if not agents:
        raise ValueError("no agents available for interactive task creation")
    print("Interactive task creation", file=sys.stderr)
    assignment = input("Assignment: ").strip()
    if not assignment:
        raise ValueError("assignment is required")
    print("Agents:", file=sys.stderr)
    for index, agent_id in enumerate(agents, start=1):
        print(f"{index}. {agent_id}", file=sys.stderr)
    selected = input(f"Assignee [1-{len(agents)}]: ").strip() or "1"
    agent = agents[int(selected) - 1]
    priority = (input("Priority [low/normal/high/urgent] (normal): ").strip() or "normal").lower()
    estimated_cycles = int(input("Estimated cycles (1): ").strip() or "1")
    goal_statement = input("Goal statement (optional): ").strip() or None
    rationale = input("Assignment rationale (optional): ").strip() or None
    depends_on_raw = input("Dependencies as comma-separated assignment ids (optional): ").strip()
    depends_on = [item.strip() for item in depends_on_raw.split(",") if item.strip()]
    return {
        "agent": agent,
        "assignment": assignment,
        "priority": priority,
        "estimated_cycles": estimated_cycles,
        "goal_statement": goal_statement,
        "rationale": rationale,
        "depends_on": depends_on,
        "idempotency_key": None,
        "created_by": user.username if user else "human",
        "source": "interactive_cli",
        "work_mode": WorkMode.HEARTBEAT.value,
    }


def _inspect_assignment(store: StateStore, assignment_id: str) -> dict[str, Any]:
    active = next(
        (item for item in store.assignments() if item.assignment_id == assignment_id),
        None,
    )
    history = next(
        (item for item in store.assignment_history() if item["assignment_id"] == assignment_id),
        None,
    )
    record = active.to_dict() if active else history["record"] if history else None
    if record is None:
        raise ValueError(f"unknown assignment: {assignment_id}")
    related_reasoning = [
        item
        for item in store.orchestrator_reasoning()
        if assignment_id in item.get("assigned", [])
        or assignment_id in item.get("queued_assignments", [])
    ]
    reasoning_summary = (
        related_reasoning[-1]["decision_summary"]
        if related_reasoning
        else "direct assignment or no reasoning record"
    )
    return {
        "assignment": record,
        "history": history,
        "related_reasoning": related_reasoning,
        "why_this_agent": record.get("assignment_rationale") or reasoning_summary,
        "goal_statement": record.get("goal_statement"),
        "dependencies": record.get("dependency_ids", []),
        "mission_statement": store.mission().statement if store.mission() else None,
    }


def _chat_metadata(store: StateStore, actor: AuthResult, sender: str) -> dict[str, Any]:
    sender_user = _find_user(store, sender)
    effective_user = sender_user or actor.user
    metadata: dict[str, Any] = {}
    if effective_user is not None:
        metadata["verified_user"] = effective_user.to_dict()
        metadata["identity_context"] = build_user_identity_context(effective_user)
    if actor.user is not None:
        metadata["actor"] = actor.user.to_dict()
        metadata["auth_method"] = actor.method
    return metadata


if __name__ == "__main__":
    raise SystemExit(main())
