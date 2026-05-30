from __future__ import annotations

import curses
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from brigade.services import build_chat_payload, build_settings_payload
from brigade.store import StateStore

VIEWS = ("mission", "goals", "tasks", "agents", "teams", "alerts")
MAX_PLAIN_LINE = 240


@dataclass(frozen=True)
class ChatTuiCommand:
    action: str
    argument: str | None = None


def parse_chat_tui_command(message: str) -> ChatTuiCommand | None:
    stripped = message.strip()
    if not stripped.startswith("/"):
        return None
    command, _, argument = stripped.partition(" ")
    action = command[1:].lower()
    aliases = {"q": "quit", "switch": "agent"}
    return ChatTuiCommand(action=aliases.get(action, action), argument=argument.strip() or None)


def _plain_line(value: object, *, limit: int = MAX_PLAIN_LINE) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _safe_addnstr(screen: Any, row: int, col: int, text: object, width: int) -> None:
    if row < 0 or col < 0 or width <= col + 1:
        return
    screen.addnstr(row, col, str(text), max(1, width - col - 1))


def build_dashboard_payload(store: StateStore) -> dict[str, Any]:
    mission = store.mission()
    reasoning = store.orchestrator_reasoning()
    latest_reasoning = reasoning[-1] if reasoning else None
    assignments = [item.to_dict() for item in store.assignments()]
    history = store.assignment_history()
    teams = [team.to_dict() for team in store.teams()]
    goals = {
        agent_id: [goal.to_dict() for goal in goals]
        for agent_id, goals in store.goals().items()
    }
    team_statuses: dict[str, dict[str, int]] = {}
    for team in teams:
        members = set(team.get("members", []))
        team_statuses[team["team_id"]] = {
            "goals": sum(len(values) for agent_id, values in goals.items() if agent_id in members),
            "active_assignments": sum(
                1 for assignment in assignments if assignment["assigned_to"] in members
            ),
            "blockers": sum(
                1
                for assignment in assignments
                if assignment["assigned_to"] in members
                and (assignment["blockers"] or assignment["status"] == "blocked")
            ),
        }
    return {
        "mission": {
            "statement": mission.statement if mission else "not set",
            "success_criteria": mission.success_criteria if mission else [],
            "explicitly_not": mission.explicitly_not if mission else [],
            "latest_reasoning": latest_reasoning["decision_summary"] if latest_reasoning else None,
            "latest_cycle_id": latest_reasoning["cycle_id"] if latest_reasoning else None,
        },
        "goals": goals,
        "tasks": {
            "active": assignments,
            "history": history[-10:],
        },
        "agents": [
            {
                **agent.to_dict(),
                "state": store.agent_states().get(agent.agent_id).to_dict()
                if store.agent_states().get(agent.agent_id)
                else None,
            }
            for agent in store.agents()
        ],
        "teams": teams,
        "team_statuses": team_statuses,
        "alerts": store.alerts(),
        "financial_report": store.latest_financial_report(),
    }


def render_dashboard_view(payload: dict[str, Any], view: str = "mission") -> str:
    if view == "mission":
        mission = payload["mission"]
        lines = [
            "Mission",
            "",
            _plain_line(mission["statement"]),
            "",
            "Success Criteria:",
        ]
        lines.extend(_plain_line(f"- {item}") for item in mission["success_criteria"])
        lines.append("")
        lines.append("Explicitly Not:")
        lines.extend(_plain_line(f"- {item}") for item in mission["explicitly_not"])
        if mission["latest_reasoning"]:
            lines.extend(
                [
                    "",
                    _plain_line(f"Latest Reasoning: {mission['latest_reasoning']}"),
                    f"Latest Cycle: {mission['latest_cycle_id']}",
                ]
            )
        return "\n".join(lines)

    if view == "goals":
        lines = ["Goals", ""]
        goals = payload["goals"]
        if not goals:
            return "Goals\n\nNo goals."
        for agent_id, values in goals.items():
            lines.append(f"{agent_id}:")
            for goal in values:
                lines.append(f"- {_plain_line(goal['statement'])}")
                if goal["success_criteria"]:
                    lines.append(_plain_line(f"  success: {', '.join(goal['success_criteria'])}"))
        return "\n".join(lines)

    if view == "tasks":
        lines = ["Tasks", ""]
        active = payload["tasks"]["active"]
        history = payload["tasks"]["history"]
        lines.append("Active:")
        if active:
            for item in active:
                lines.append(
                    _plain_line(
                        f"- {item['assignment_id']} {item['status']} "
                        f"{item['assigned_to']}: {item['assignment']}"
                    )
                )
        else:
            lines.append("- none")
        lines.append("")
        lines.append("Recent History:")
        if history:
            for item in history:
                lines.append(
                    _plain_line(
                        f"- {item['assignment_id']} {item['final_status']}: "
                        f"{item['executive_summary']}"
                    )
                )
        else:
            lines.append("- none")
        return "\n".join(lines)

    if view == "agents":
        lines = ["Agents", ""]
        for agent in payload["agents"]:
            state = agent["state"] or {}
            lines.append(f"{agent['agent_id']} ({agent['role']})")
            lines.append(f"  workspace: {agent['workspace_path']}")
            lines.append(f"  status: {state.get('status', 'idle')}")
            if state.get("current_assignment_summary"):
                lines.append(_plain_line(f"  task: {state['current_assignment_summary']}"))
            if state.get("assignment_progress"):
                lines.append(_plain_line(f"  progress: {state['assignment_progress']}"))
        return "\n".join(lines)

    if view == "teams":
        lines = ["Teams", ""]
        teams = payload["teams"]
        agents = {agent["agent_id"]: agent for agent in payload["agents"]}
        if not teams:
            lines.append("No teams.")
            return "\n".join(lines)
        for team in teams:
            chief = team.get("crew_chief_id") or "none"
            status = payload.get("team_statuses", {}).get(team["team_id"], {})
            lines.append(f"{team['team_id']} ({team['display_name']})")
            lines.append(f"  chief: {chief}")
            lines.append(f"  policy: {team.get('delegation_policy', 'chief_only')}")
            if team.get("escalation_team_id"):
                lines.append(f"  escalation: {team['escalation_team_id']}")
            if team.get("parent_team_id"):
                lines.append(f"  parent: {team['parent_team_id']}")
            lines.append(
                "  status: "
                f"goals={status.get('goals', 0)} "
                f"active={status.get('active_assignments', 0)} "
                f"blockers={status.get('blockers', 0)}"
            )
            members = team.get("members", [])
            if members:
                lines.append("  members:")
                for member in members:
                    agent = agents.get(member)
                    role = agent["role"] if agent else "unknown"
                    lines.append(f"  - {member} ({role})")
            else:
                lines.append("  members: none")
        return "\n".join(lines)

    if view == "alerts":
        lines = ["Alerts", ""]
        alerts = payload["alerts"]
        if not alerts:
            lines.append("No alerts.")
        else:
            lines.extend(_plain_line(f"- {item}") for item in alerts)
        report = payload.get("financial_report")
        if report:
            lines.extend(
                [
                    "",
                    "Financial Report:",
                    json.dumps(report, indent=2, sort_keys=True),
                ]
            )
        return "\n".join(lines)

    raise ValueError(f"unknown dashboard view: {view}")


def run_dashboard_tui(
    load_payload: Callable[[], dict[str, Any]],
    refresh_seconds: float = 2.0,
) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("dashboard TUI requires a real TTY")
    return curses.wrapper(lambda screen: _dashboard_loop(screen, load_payload, refresh_seconds))


def _dashboard_loop(
    screen: Any,
    load_payload: Callable[[], dict[str, Any]],
    refresh_seconds: float,
) -> int:
    curses.curs_set(0)
    screen.nodelay(True)
    screen.keypad(True)
    current_view = 0
    next_refresh = 0.0
    payload = load_payload()
    while True:
        now = time.time()
        if now >= next_refresh:
            payload = load_payload()
            next_refresh = now + refresh_seconds
        screen.erase()
        height, width = screen.getmaxyx()
        if height <= 0 or width <= 0:
            time.sleep(0.05)
            continue
        header = (
            "OpenBrigade Dashboard "
            "[1 mission] [2 goals] [3 tasks] [4 agents] [5 teams] [6 alerts] "
            "[r refresh] [q quit]"
        )
        _safe_addnstr(screen, 0, 0, header, width)
        _safe_addnstr(screen, 1, 0, f"View: {VIEWS[current_view]}", width)
        body = render_dashboard_view(payload, VIEWS[current_view]).splitlines()
        for row, line in enumerate(body, start=3):
            if row >= height:
                break
            _safe_addnstr(screen, row, 0, line, width)
        screen.refresh()
        key = screen.getch()
        if key in (ord("q"), ord("Q")):
            return 0
        if key in (ord("r"), ord("R")):
            payload = load_payload()
            next_refresh = time.time() + refresh_seconds
        if key in (ord("1"), ord("2"), ord("3"), ord("4"), ord("5"), ord("6")):
            current_view = int(chr(key)) - 1
        time.sleep(0.05)


def render_chat_view(payload: dict[str, Any], selected_channel: str | None = None) -> str:
    channel = selected_channel or payload.get("selected_channel") or "all channels"
    lines = ["Chat", "", f"Channel: {channel}", ""]
    messages = payload.get("messages", [])
    if not messages:
        lines.append("No messages.")
        return "\n".join(lines)
    for message in messages:
        lines.append(
            _plain_line(
                f"{message['created_at']} {message['sender']} -> "
                f"{message['recipient']}: {message['content']}"
            )
        )
    return "\n".join(lines)


def run_chat_tui(
    store: StateStore,
    send_message: Callable[[str, str | None, str | None], dict[str, Any]],
    *,
    channel: str | None = None,
    agent_id: str | None = None,
    agent_ids: list[str] | None = None,
    channel_for_agent: Callable[[str], str] | None = None,
    refresh_seconds: float = 1.0,
) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("chat TUI requires a real TTY")
    return curses.wrapper(
        lambda screen: _chat_loop(
            screen,
            store,
            send_message,
            channel,
            agent_id,
            agent_ids or [],
            channel_for_agent,
            refresh_seconds,
        )
    )


def _chat_loop(
    screen: Any,
    store: StateStore,
    send_message: Callable[[str, str | None, str | None], dict[str, Any]],
    channel: str | None,
    agent_id: str | None,
    agent_ids: list[str],
    channel_for_agent: Callable[[str], str] | None,
    refresh_seconds: float,
) -> int:
    curses.curs_set(1)
    screen.nodelay(False)
    screen.keypad(True)
    draft = ""
    active_agent_id = agent_id
    active_channel = channel or (
        channel_for_agent(active_agent_id) if active_agent_id and channel_for_agent else channel
    )
    status = "enter sends, /agent <id> switches, /agents lists, /quit exits"
    next_refresh = 0.0
    payload = build_chat_payload(store, channel=active_channel)
    while True:
        now = time.time()
        if now >= next_refresh:
            payload = build_chat_payload(store, channel=active_channel)
            next_refresh = now + refresh_seconds
        screen.erase()
        height, width = screen.getmaxyx()
        if height <= 0 or width <= 0:
            continue
        header = (
            "OpenBrigade Chat "
            f"[agent: {active_agent_id or 'none'}] "
            f"[channel: {active_channel or 'all'}] "
            "[/agent <id>] [/agents] [/refresh] [/quit]"
        )
        _safe_addnstr(screen, 0, 0, header, width)
        body = render_chat_view(payload, active_channel).splitlines()
        input_row = max(3, height - 2)
        for row, line in enumerate(body[-(input_row - 2) :], start=1):
            if row >= input_row:
                break
            _safe_addnstr(screen, row, 0, line, width)
        _safe_addnstr(screen, input_row, 0, f"> {draft}", width)
        _safe_addnstr(screen, input_row + 1, 0, status, width)
        screen.refresh()
        key = screen.getch()
        if key in (10, 13):
            message = draft.strip()
            draft = ""
            command = parse_chat_tui_command(message)
            if command is not None:
                if command.action == "quit":
                    return 0
                if command.action == "refresh":
                    payload = build_chat_payload(store, channel=active_channel)
                    status = "refreshed"
                    continue
                if command.action == "agents":
                    status = "agents: " + ", ".join(agent_ids)
                    continue
                if command.action == "help":
                    status = (
                        "/agent <id> switches target, /agents lists, "
                        "/refresh reloads, /quit exits"
                    )
                    continue
                if command.action == "agent":
                    requested = command.argument
                    if not requested:
                        status = "usage: /agent <id>"
                        continue
                    if agent_ids and requested not in agent_ids:
                        status = f"unknown agent: {requested}"
                        continue
                    active_agent_id = requested
                    active_channel = channel or (
                        channel_for_agent(requested) if channel_for_agent else active_channel
                    )
                    payload = build_chat_payload(store, channel=active_channel)
                    status = f"switched to {requested}"
                    continue
                status = f"unknown command: /{command.action}"
                continue
            if message:
                if not active_agent_id:
                    status = "select an agent with /agent <id>"
                    continue
                try:
                    result = send_message(message, active_agent_id, active_channel)
                    status = f"sent: {result.get('status')}"
                    payload = build_chat_payload(store, channel=active_channel)
                except Exception as exc:  # pragma: no cover - interactive safety
                    status = f"send failed: {exc}"
            continue
        if key in (27,):
            return 0
        if key in (curses.KEY_BACKSPACE, 127, 8):
            draft = draft[:-1]
            continue
        if 32 <= key <= 126:
            draft += chr(key)


def render_settings_view(payload: dict[str, Any]) -> str:
    lines = ["Settings", ""]
    for key in sorted(payload):
        value = payload[key]
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        lines.append(_plain_line(f"{key}: {value}"))
    return "\n".join(lines)


def run_settings_tui(
    load_payload: Callable[[], dict[str, Any]],
    refresh_seconds: float = 2.0,
) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("settings TUI requires a real TTY")
    return curses.wrapper(lambda screen: _settings_loop(screen, load_payload, refresh_seconds))


def _settings_loop(
    screen: Any,
    load_payload: Callable[[], dict[str, Any]],
    refresh_seconds: float,
) -> int:
    curses.curs_set(0)
    screen.nodelay(True)
    payload = load_payload()
    next_refresh = 0.0
    while True:
        now = time.time()
        if now >= next_refresh:
            payload = load_payload()
            next_refresh = now + refresh_seconds
        screen.erase()
        height, width = screen.getmaxyx()
        if height <= 0 or width <= 0:
            time.sleep(0.05)
            continue
        _safe_addnstr(screen, 0, 0, "OpenBrigade Settings [r refresh] [q quit]", width)
        body = render_settings_view(payload).splitlines()
        for row, line in enumerate(body, start=2):
            if row >= height:
                break
            _safe_addnstr(screen, row, 0, line, width)
        screen.refresh()
        key = screen.getch()
        if key in (ord("q"), ord("Q")):
            return 0
        if key in (ord("r"), ord("R")):
            payload = load_payload()
            next_refresh = time.time() + refresh_seconds
        time.sleep(0.05)


def default_chat_payload(store: StateStore, channel: str | None) -> dict[str, Any]:
    return build_chat_payload(store, channel=channel)


def default_settings_payload(settings: Any) -> dict[str, Any]:
    return build_settings_payload(settings)
