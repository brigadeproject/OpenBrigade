from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from brigade.finance import persist_financial_report
from brigade.orchestrator import orchestration_event, record_orchestration_events
from brigade.prompt_floors import build_agent_floor, compact_json
from brigade.providers import ModelProvider, ModelResponse, ModelUnavailableError
from brigade.schemas import (
    MALFORMED_PROVIDER_OUTPUT_MARKER,
    AgentState,
    Assignment,
    AssignmentKind,
    AssignmentStatus,
    extract_json_object,
)
from brigade.store import StateStore
from brigade.time import add_seconds_iso, parse_utc_iso, utc_now, utc_now_iso
from brigade.tools import (
    ToolContext,
    ToolRegistry,
    default_tool_registry,
)
from brigade.workspace import (
    REQUIRED_AGENT_FILES,
    HeartbeatValidationError,
    parse_heartbeat_assignment_block,
    write_heartbeat_assignment,
)

LOCAL_INFERENCE_LOCK_TTL_SECONDS = 600
LOCAL_INFERENCE_RELEASE_COOLDOWN_SECONDS = 0
LOCAL_INFERENCE_BACKPRESSURE_PREFIXES = (
    "local inference unavailable until ",
    "local inference already held by ",
)
MAX_PROVIDER_RETRIES = 3
MAX_AGENT_ITERATIONS = 6
MAX_CONTEXT_FILE_CHARS = 4000
MAX_KNOWLEDGE_SNIPPETS = 3
TRANSIENT_ERROR_HINTS = (
    "timeout",
    "timed out",
    "connection reset",
    "temporarily unavailable",
    "unreachable",
    "refused",
    "5",
)
LOGGER = logging.getLogger(__name__)


class MalformedProviderOutput(ValueError):
    pass


@dataclass(frozen=True)
class ParsedAgentResponse:
    status: str
    summary: str
    blockers: list[str]
    awaiting_human: bool = False
    tool_name: str | None = None
    tool_arguments: dict[str, Any] | None = None
    expected_next_activity_at: str | None = None


@dataclass(frozen=True)
class RunResult:
    assignment_id: str
    status: str
    summary: str
    route_type: str
    transcript_path: str | None = None
    cycle_count: int = 0

    def to_dict(self) -> dict[str, str | int | None]:
        return self.__dict__.copy()


def run_managed_agents(
    store: StateStore,
    provider: ModelProvider,
    agent_id: str | None = None,
    tool_registry: ToolRegistry | None = None,
    provider_factory: Callable[[str], ModelProvider] | None = None,
    fallback_provider: ModelProvider | None = None,
) -> list[RunResult]:
    target_ids = [agent_id] if agent_id else [item.agent_id for item in store.agents()]
    results: list[RunResult] = []
    registry = tool_registry or default_tool_registry()
    for current_agent_id in target_ids:
        assignment = store.active_assignment_for_agent(current_agent_id)
        if assignment is None:
            continue
        run_provider = provider_factory(current_agent_id) if provider_factory else provider
        result_provider = run_provider
        try:
            results.append(
                run_agent_once(
                    current_agent_id,
                    store,
                    run_provider,
                    tool_registry=registry,
                )
            )
        except RuntimeError as exc:
            backpressure = is_local_inference_backpressure(exc)
            if (
                fallback_provider is not None
                and _provider_key(fallback_provider) != _provider_key(run_provider)
                and not backpressure
            ):
                store.add_alert(
                    f"agent {current_agent_id} provider {_provider_key(run_provider)} failed: "
                    f"{exc}; retrying with default provider {_provider_key(fallback_provider)}"
                )
                LOGGER.warning(
                    "agent_run_provider_fallback",
                    extra={
                        "agent_id": current_agent_id,
                        "assignment_id": assignment.assignment_id,
                        "provider": _provider_key(run_provider),
                        "fallback_provider": _provider_key(fallback_provider),
                        "reason": str(exc),
                    },
                )
                try:
                    result_provider = fallback_provider
                    results.append(
                        run_agent_once(
                            current_agent_id,
                            store,
                            fallback_provider,
                            tool_registry=registry,
                        )
                    )
                    continue
                except RuntimeError as fallback_exc:
                    exc = fallback_exc
                    backpressure = is_local_inference_backpressure(exc)
            summary = str(exc)
            if not backpressure:
                store.add_alert(f"agent {current_agent_id} run deferred: {summary}")
            log = LOGGER.info if backpressure else LOGGER.warning
            log(
                "agent_run_deferred_local_backpressure" if backpressure else "agent_run_deferred",
                extra={
                    "agent_id": current_agent_id,
                    "assignment_id": assignment.assignment_id,
                    "reason": summary,
                },
            )
            results.append(
                RunResult(
                    assignment_id=assignment.assignment_id,
                    status=assignment.status.value,
                    summary=summary,
                    route_type=getattr(result_provider, "route_type", "unknown"),
                    transcript_path=assignment.transcript_path,
                    cycle_count=assignment.cycle_count,
                )
            )
    return results


def is_local_inference_backpressure(exc: BaseException | str) -> bool:
    summary = str(exc)
    return summary.startswith(LOCAL_INFERENCE_BACKPRESSURE_PREFIXES)


def _provider_key(provider: ModelProvider) -> str:
    return (
        f"{getattr(provider, 'provider_name', provider.__class__.__name__)}:"
        f"{getattr(provider, 'model', 'unknown')}:"
        f"{getattr(provider, 'route_type', 'unknown')}"
    )


def run_agent_once(
    agent_id: str,
    store: StateStore,
    provider: ModelProvider,
    *,
    tool_registry: ToolRegistry | None = None,
) -> RunResult:
    agent = next((item for item in store.agents() if item.agent_id == agent_id), None)
    if agent is None:
        raise ValueError(f"unknown agent: {agent_id}")

    heartbeat = store.data_dir / agent.workspace_path / "HEARTBEAT.md"
    route_type = getattr(provider, "route_type", "local")
    assignment = store.active_assignment_for_agent(agent_id)
    if assignment is None:
        raise ValueError(f"no active assignment for agent: {agent_id}")
    registry = tool_registry or default_tool_registry()

    try:
        heartbeat_assignment = parse_heartbeat_assignment_block(
            heartbeat.read_text(encoding="utf-8"),
            expected_agent_id=agent_id,
        ).assignment
        if heartbeat_assignment.assignment_id != assignment.assignment_id:
            raise HeartbeatValidationError(
                "stale_assignment_id",
                "heartbeat assignment_id does not match the active stored assignment",
                assignment_id=heartbeat_assignment.assignment_id,
                assigned_to=heartbeat_assignment.assigned_to,
            )
    except HeartbeatValidationError as exc:
        return _handle_heartbeat_validation_failure(
            agent_id,
            assignment,
            agent,
            store,
            route_type,
            str(exc),
        )

    run_owner = str(uuid4())
    execution_claim_acquired = store.try_claim_assignment_execution(
        assignment.assignment_id,
        run_owner,
        agent_id=agent_id,
    )
    if not execution_claim_acquired:
        claim = store.assignment_execution_claim(assignment.assignment_id)
        holder = claim.get("owner") if claim else "another runner"
        return RunResult(
            assignment_id=assignment.assignment_id,
            status=assignment.status.value,
            summary=f"assignment already being executed by {holder}",
            route_type=route_type,
            transcript_path=assignment.transcript_path,
            cycle_count=assignment.cycle_count,
        )

    if route_type == "cloud":
        active_cloud_jobs = [
            job for job in store.cloud_jobs() if job.get("status") not in {"complete", "failed"}
        ]
        if active_cloud_jobs:
            message = "cloud dispatch blocked while another cloud job is already in flight"
            assignment.register_failure(message, blockers=[message], awaiting_human=False)
            write_heartbeat_assignment(agent, assignment, store.data_dir)
            store.update_assignment(assignment)
            store.upsert_agent_state(
                AgentState(
                    agent=agent.agent_id,
                    status="blocked",
                    current_assignment_id=assignment.assignment_id,
                    current_assignment_summary=assignment.assignment,
                    assignment_progress=assignment.progress_summary,
                    blockers=assignment.blockers,
                )
            )
            store.add_alert(f"assignment {assignment.assignment_id}: {message}")
            return RunResult(
                assignment_id=assignment.assignment_id,
                status=assignment.status.value,
                summary=message,
                route_type=route_type,
                cycle_count=assignment.cycle_count,
            )

    cloud_job: dict[str, object] | None = None
    lock_acquired = False
    local_release_cooldown_seconds = LOCAL_INFERENCE_RELEASE_COOLDOWN_SECONDS
    try:
        if route_type == "local":
            _acquire_local_inference_lock(store, agent_id)
            lock_acquired = True

        if route_type == "cloud":
            cloud_job = {
                "job_id": str(uuid4()),
                "assignment_id": assignment.assignment_id,
                "agent_id": agent.agent_id,
                "provider": getattr(provider, "__class__", type(provider)).__name__,
                "model": getattr(provider, "model", "unknown"),
                "status": "running",
                "transcript_path": None,
                "started_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            }
            store.upsert_cloud_job(cloud_job)

        assignment.record_run(
            provider=getattr(provider, "__class__", type(provider))
            .__name__.replace("Provider", "")
            .lower(),
            model=getattr(provider, "model", "unknown"),
        )
        responses, parsed, observations = _complete_assignment_with_tools(
            agent,
            assignment,
            store,
            provider,
            registry,
        )
        LOGGER.info(
            "agent_run_completed",
            extra={
                "agent_id": agent_id,
                "assignment_id": assignment.assignment_id,
                "status": parsed.status,
                "iterations": len(responses),
                "tool_calls": len(observations),
            },
        )
        response = responses[-1]
        transcript_path = _write_transcript(
            store.data_dir,
            agent_id,
            assignment,
            build_assignment_prompt(agent, assignment, store, registry, observations=observations),
            responses,
            observations,
        )
        assignment.transcript_path = str(transcript_path)
        store.add_transcript(
            {
                "transcript_id": str(uuid4()),
                "assignment_id": assignment.assignment_id,
                "agent_id": agent_id,
                "provider": response.provider,
                "model": response.model,
                "route_type": response.route_type,
                "path": str(transcript_path),
                "created_at": utc_now_iso(),
            }
        )
        for index, response_item in enumerate(responses, start=1):
            store.add_usage_record(
                {
                    "usage_id": str(uuid4()),
                    "assignment_id": assignment.assignment_id,
                    "agent_id": agent_id,
                    "provider": response_item.provider,
                    "model": response_item.model,
                    "route_type": response_item.route_type,
                    "input_tokens": response_item.input_tokens,
                    "output_tokens": response_item.output_tokens,
                    "total_tokens": response_item.input_tokens + response_item.output_tokens,
                    "estimated_cost_usd": response_item.estimated_cost_usd,
                    "recorded_at": utc_now_iso(),
                    "iteration": index,
                }
            )
        result = _apply_agent_response(
            agent_id, assignment, parsed, agent, store, response.route_type
        )
        if cloud_job is not None:
            cloud_job["status"] = "complete"
            cloud_job["updated_at"] = utc_now_iso()
            cloud_job["transcript_path"] = result.transcript_path
            cloud_job["summary"] = result.summary
            store.upsert_cloud_job(cloud_job)
        persist_financial_report(store, store.data_dir)
        return result
    except Exception as exc:
        if route_type == "local" and isinstance(exc, ModelUnavailableError):
            local_release_cooldown_seconds = 0
        if cloud_job is not None:
            cloud_job["status"] = "failed"
            cloud_job["updated_at"] = utc_now_iso()
            cloud_job["error"] = str(exc)
            store.upsert_cloud_job(cloud_job)
        raise
    finally:
        if lock_acquired:
            _release_local_inference_lock(
                store,
                agent_id,
                cooldown_seconds=local_release_cooldown_seconds,
            )
        if execution_claim_acquired:
            store.release_assignment_execution_claim(
                assignment.assignment_id,
                owner=run_owner,
            )


def _complete_assignment_with_tools(
    agent,
    assignment: Assignment,
    store: StateStore,
    provider: ModelProvider,
    registry: ToolRegistry,
) -> tuple[list[ModelResponse], ParsedAgentResponse, list[dict[str, Any]]]:
    responses: list[ModelResponse] = []
    observations: list[dict[str, Any]] = []
    context = ToolContext(agent=agent, assignment=assignment, store=store)
    for _ in range(MAX_AGENT_ITERATIONS):
        prompt = build_assignment_prompt(
            agent,
            assignment,
            store,
            registry,
            observations=observations,
        )
        response = _complete_with_retries(provider, prompt)
        responses.append(response)
        parsed = parse_agent_response(response.text)
        if parsed.status != "tool_call":
            return responses, parsed, observations
        result = registry.execute(
            parsed.tool_name or "",
            context,
            parsed.tool_arguments or {},
        )
        LOGGER.info(
            "agent_tool_call",
            extra={
                "agent_id": agent.agent_id,
                "assignment_id": assignment.assignment_id,
                "tool": parsed.tool_name,
                "ok": result.ok,
            },
        )
        observations.append(result.to_observation(parsed.tool_name or "unknown"))
    exhausted = ParsedAgentResponse(
        status="working",
        summary=f"tool iteration budget exhausted after {MAX_AGENT_ITERATIONS} iterations",
        blockers=[],
    )
    return responses, exhausted, observations


def build_assignment_prompt(
    agent,
    assignment: Assignment,
    store: StateStore,
    registry: ToolRegistry | None = None,
    *,
    observations: list[dict[str, Any]] | None = None,
) -> str:
    registry = registry or default_tool_registry()
    context = build_agent_floor(
        agent,
        assignment,
        store,
        registry,
        observations=observations,
    )
    return "\n".join(
        [
            context["system_prompt"],
            "",
            "OpenBrigade agent response protocol:",
            "Return only one JSON object. Do not wrap it in Markdown.",
            "For final or checkpoint status, use:",
            (
                '{"status":"complete|working|blocked|awaiting_human|failed",'
                '"summary":"...","blockers":[],"expected_next_activity_at":"..."}'
            ),
            "To use a tool, use:",
            '{"status":"tool_call","tool":"tool_name","arguments":{},'
            '"summary":"why this tool is needed"}',
            "Use complete only when the assignment is actually done. Use working when "
            "progress was made but more cycles are needed.",
            "Use expected_next_activity_at on working responses when this task is "
            "intentionally waiting until a future UTC timestamp.",
            "Use blocked or awaiting_human when you need outside intervention.",
            "",
            "Floor JSON:",
            compact_json(context),
        ]
    )


def _workspace_context(workspace: Path) -> dict[str, str]:
    context: dict[str, str] = {}
    for filename in REQUIRED_AGENT_FILES:
        path = workspace / filename
        if not path.exists() or not path.is_file():
            context[filename] = "<missing>"
            continue
        text = path.read_text(encoding="utf-8")
        context[filename] = _truncate(text, MAX_CONTEXT_FILE_CHARS)
    return context


def _dependency_state(store: StateStore, assignment: Assignment) -> list[dict[str, Any]]:
    if not assignment.dependency_ids:
        return []
    active = {item.assignment_id: item for item in store.assignments()}
    history = {
        item.get("assignment_id"): item
        for item in store.assignment_history()
        if item.get("assignment_id")
    }
    dependencies = []
    for dependency_id in assignment.dependency_ids:
        active_assignment = active.get(dependency_id)
        if active_assignment is not None:
            dependencies.append(
                {
                    "assignment_id": dependency_id,
                    "status": active_assignment.status.value,
                    "complete": active_assignment.status == AssignmentStatus.COMPLETE,
                    "summary": active_assignment.progress_summary,
                }
            )
            continue
        archived = history.get(dependency_id)
        if archived is not None:
            dependencies.append(
                {
                    "assignment_id": dependency_id,
                    "status": archived.get("final_status"),
                    "complete": archived.get("final_status") == AssignmentStatus.COMPLETE.value,
                    "summary": archived.get("executive_summary"),
                }
            )
            continue
        dependencies.append(
            {
                "assignment_id": dependency_id,
                "status": "unknown",
                "complete": False,
                "summary": None,
            }
        )
    return dependencies


def _agent_state_context(store: StateStore, agent_id: str) -> dict[str, Any] | None:
    state = store.agent_states().get(agent_id)
    return state.to_dict() if state else None


def _knowledge_snippets(store: StateStore) -> list[dict[str, str]]:
    snippets = []
    for chunk in store.knowledge_chunks()[:MAX_KNOWLEDGE_SNIPPETS]:
        text = str(chunk.get("text") or "")
        snippets.append(
            {
                "chunk_id": str(chunk.get("chunk_id") or ""),
                "source": str(chunk.get("source") or chunk.get("content_path") or ""),
                "text": _truncate(text, 1200),
            }
        )
    return snippets


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 14].rstrip() + "\n<truncated>"


def parse_agent_response(text: str) -> ParsedAgentResponse:
    stripped = text.strip()
    if not stripped:
        raise MalformedProviderOutput("empty model response")
    candidate = extract_json_object(stripped)
    if candidate.startswith("{"):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise MalformedProviderOutput(f"invalid JSON response: {exc.msg}") from exc
        status = str(payload.get("status", "")).strip().lower()
        if status not in {
            "complete",
            "working",
            "blocked",
            "awaiting_human",
            "failed",
            "tool_call",
        }:
            raise MalformedProviderOutput(f"unsupported status: {status or '<missing>'}")
        summary = str(payload.get("summary") or payload.get("response") or "").strip()
        if not summary and status != "tool_call":
            raise MalformedProviderOutput("JSON response is missing a summary")
        blockers = payload.get("blockers", [])
        if isinstance(blockers, str):
            blockers = [blockers]
        if not isinstance(blockers, list):
            raise MalformedProviderOutput("blockers must be a list or string")
        if status == "tool_call":
            tool_name = str(payload.get("tool") or payload.get("tool_name") or "").strip()
            tool_arguments = payload.get("arguments") or payload.get("tool_arguments") or {}
            if not tool_name:
                raise MalformedProviderOutput("tool_call response is missing a tool name")
            if not isinstance(tool_arguments, dict):
                raise MalformedProviderOutput("tool_call arguments must be an object")
            return ParsedAgentResponse(
                status=status,
                summary=summary or f"requested tool {tool_name}",
                blockers=[],
                tool_name=tool_name,
                tool_arguments=tool_arguments,
            )
        expected_next_activity_at = _optional_utc_timestamp(
            payload.get("expected_next_activity_at")
        )
        return ParsedAgentResponse(
            status=status,
            summary=summary,
            blockers=[str(item).strip() for item in blockers if str(item).strip()],
            awaiting_human=bool(payload.get("awaiting_human")) or status == "awaiting_human",
            expected_next_activity_at=expected_next_activity_at,
        )
    return ParsedAgentResponse(status="working", summary=stripped, blockers=[])


def _optional_utc_timestamp(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise MalformedProviderOutput("expected_next_activity_at must be a string")
    try:
        return parse_utc_iso(value).isoformat()
    except ValueError as exc:
        raise MalformedProviderOutput(
            "expected_next_activity_at must be a UTC ISO timestamp"
        ) from exc


def _apply_agent_response(
    agent_id: str,
    assignment: Assignment,
    parsed: ParsedAgentResponse,
    agent,
    store: StateStore,
    route_type: str,
) -> RunResult:
    if parsed.status == "complete":
        assignment.mark_complete(parsed.summary)
        write_heartbeat_assignment(agent, assignment, store.data_dir)
        if assignment.kind == AssignmentKind.REST:
            # Deterministic dream finalizer: durable output regardless of
            # model quality.
            from brigade.rest import finalize_rest_assignment

            finalize_rest_assignment(store, agent, assignment)
        _record_parent_synthesis_if_needed(store, assignment, parsed.summary)
        store.archive_assignment(assignment, executive_summary=parsed.summary)
        store.upsert_agent_state(
            AgentState(
                agent=agent_id,
                status="idle",
                last_completed=parsed.summary,
            )
        )
        return RunResult(
            assignment_id=assignment.assignment_id,
            status=assignment.status.value,
            summary=parsed.summary,
            route_type=route_type,
            transcript_path=assignment.transcript_path,
            cycle_count=assignment.cycle_count,
        )

    if parsed.status == "working":
        assignment.mark_cycle_incomplete(summary=parsed.summary, blockers=parsed.blockers)
        if parsed.expected_next_activity_at:
            assignment.checkpoint_at = parsed.expected_next_activity_at
        write_heartbeat_assignment(agent, assignment, store.data_dir)
        if assignment.status == AssignmentStatus.ABANDONED:
            abandoned_summary = (
                f"abandoned after {assignment.cycle_count} cycles: {parsed.summary}"
            )
            store.archive_assignment(
                assignment,
                executive_summary=abandoned_summary,
            )
            store.add_alert(
                f"assignment {assignment.assignment_id} "
                f"abandoned after {assignment.cycle_count} cycles"
            )
            store.upsert_agent_state(
                AgentState(
                    agent=agent_id,
                    status="blocked",
                    last_completed=f"abandoned: {parsed.summary}",
                    blockers=parsed.blockers,
                )
            )
        else:
            store.update_assignment(assignment)
            store.upsert_agent_state(
                AgentState(
                    agent=agent_id,
                    status="working",
                    current_assignment_id=assignment.assignment_id,
                    current_assignment_summary=assignment.assignment,
                    assignment_progress=parsed.summary,
                    blockers=parsed.blockers,
                    next_available=assignment.checkpoint_at or "after_current_assignment",
                )
            )
        return RunResult(
            assignment_id=assignment.assignment_id,
            status=assignment.status.value,
            summary=parsed.summary,
            route_type=route_type,
            transcript_path=assignment.transcript_path,
            cycle_count=assignment.cycle_count,
        )

    assignment.register_failure(
        parsed.summary,
        blockers=parsed.blockers,
        awaiting_human=parsed.awaiting_human,
    )
    write_heartbeat_assignment(agent, assignment, store.data_dir)
    store.update_assignment(assignment)
    state_status = "awaiting_human" if assignment.awaiting_human else "blocked"
    store.upsert_agent_state(
        AgentState(
            agent=agent_id,
            status=state_status,
            current_assignment_id=assignment.assignment_id,
            current_assignment_summary=assignment.assignment,
            assignment_progress=parsed.summary,
            blockers=assignment.blockers,
        )
    )
    if assignment.awaiting_human:
        store.add_alert(
            f"assignment {assignment.assignment_id} requires human intervention "
            f"after {assignment.consecutive_failures} failures"
        )
    return RunResult(
        assignment_id=assignment.assignment_id,
        status=assignment.status.value,
        summary=parsed.summary,
        route_type=route_type,
        transcript_path=assignment.transcript_path,
        cycle_count=assignment.cycle_count,
    )


def _record_parent_synthesis_if_needed(
    store: StateStore,
    assignment: Assignment,
    summary: str,
) -> None:
    child_assignments = [
        item
        for item in store.assignments()
        if item.parent_assignment_id == assignment.assignment_id
    ]
    child_history = [
        item
        for item in store.assignment_history()
        if (item.get("record") or {}).get("parent_assignment_id") == assignment.assignment_id
    ]
    child_ids = [
        *[item.assignment_id for item in child_assignments],
        *[
            str(item.get("assignment_id"))
            for item in child_history
            if item.get("assignment_id") is not None
        ],
    ]
    if not child_ids:
        return
    mission = store.mission()
    record_orchestration_events(
        store,
        source="parent_synthesis",
        decision_summary=(
            f"parent {assignment.assignment_id} completed with {len(child_ids)} child assignment(s)"
        ),
        mission_statement=mission.statement if mission else None,
        events=[
            orchestration_event(
                "parent_synthesis",
                (
                    f"Parent assignment {assignment.assignment_id} completed after "
                    f"{len(child_ids)} child assignment(s)."
                ),
                source="parent_synthesis",
                decision="completed",
                status=assignment.status.value,
                mission_statement=mission.statement if mission else None,
                goal_statement=assignment.goal_statement,
                assignment_id=assignment.assignment_id,
                assignment_ids=[assignment.assignment_id],
                agent_id=assignment.assigned_to,
                parent_assignment_id=assignment.assignment_id,
                child_assignment_ids=child_ids,
                idempotency_key=assignment.idempotency_key,
                payload={
                    "parent_assignment": assignment.to_dict(),
                    "child_assignment_ids": child_ids,
                    "summary": summary,
                },
            )
        ],
    )


def _handle_heartbeat_validation_failure(
    agent_id: str,
    assignment: Assignment,
    agent,
    store: StateStore,
    route_type: str,
    message: str,
) -> RunResult:
    assignment.register_failure(message, blockers=[message], awaiting_human=False)
    store.update_assignment(assignment)
    store.upsert_agent_state(
        AgentState(
            agent=agent_id,
            status="blocked",
            current_assignment_id=assignment.assignment_id,
            current_assignment_summary=assignment.assignment,
            assignment_progress=message,
            blockers=assignment.blockers,
        )
    )
    store.add_alert(f"assignment {assignment.assignment_id}: {message}")
    LOGGER.warning(
        "heartbeat_validation_failed",
        extra={"agent_id": agent_id, "assignment_id": assignment.assignment_id},
    )
    return RunResult(
        assignment_id=assignment.assignment_id,
        status=assignment.status.value,
        summary=message,
        route_type=route_type,
        transcript_path=assignment.transcript_path,
        cycle_count=assignment.cycle_count,
    )


def _complete_with_retries(provider: ModelProvider, prompt: str) -> ModelResponse:
    last_error: Exception | None = None
    last_response: ModelResponse | None = None
    attempt_prompt = prompt
    for attempt in range(1, MAX_PROVIDER_RETRIES + 1):
        try:
            response = provider.complete(attempt_prompt)
            last_response = response
            parse_agent_response(response.text)
            return response
        except (MalformedProviderOutput, RuntimeError) as exc:
            last_error = exc
            retryable = isinstance(exc, MalformedProviderOutput) or _is_transient_provider_error(
                exc
            )
            if not retryable or attempt >= MAX_PROVIDER_RETRIES:
                break
            if isinstance(exc, MalformedProviderOutput):
                # Feed the parse failure back so the retry has a chance to
                # self-correct instead of reproducing the same bad output.
                attempt_prompt = _prompt_with_correction(prompt, exc, last_response)
    if last_error is not None:
        if isinstance(last_error, MalformedProviderOutput) and last_response is not None:
            return ModelResponse(
                text=json.dumps(
                    {
                        "status": "blocked",
                        "summary": (
                            f"{MALFORMED_PROVIDER_OUTPUT_MARKER} after "
                            f"{MAX_PROVIDER_RETRIES} attempts: {last_error}"
                        ),
                        "blockers": [str(last_error)],
                    }
                ),
                input_tokens=last_response.input_tokens,
                output_tokens=last_response.output_tokens,
                provider=last_response.provider,
                model=last_response.model,
                route_type=last_response.route_type,
                estimated_cost_usd=last_response.estimated_cost_usd,
            )
        raise last_error
    raise RuntimeError("provider completion failed without an error")


def _prompt_with_correction(
    prompt: str,
    exc: MalformedProviderOutput,
    last_response: ModelResponse | None,
) -> str:
    bad_output = _truncate(last_response.text, 800) if last_response is not None else "<empty>"
    return "\n".join(
        [
            prompt,
            "",
            "Your previous response could not be parsed:",
            f"Error: {exc}",
            "Previous response:",
            bad_output,
            "",
            "Reply again with exactly one raw JSON object matching the protocol "
            "above. Do not wrap it in Markdown or add any other text.",
        ]
    )


def _is_transient_provider_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(hint in message for hint in TRANSIENT_ERROR_HINTS)


def _write_transcript(
    data_dir: Path,
    agent_id: str,
    assignment: Assignment,
    prompt: str,
    responses: list[ModelResponse],
    observations: list[dict[str, Any]],
) -> Path:
    final_response = responses[-1]
    transcripts_dir = data_dir / "transcripts" / agent_id
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    path = transcripts_dir / f"{assignment.assignment_id}.md"
    response_sections: list[str] = []
    for index, response in enumerate(responses, start=1):
        response_sections.extend(
            [
                f"## Response {index}",
                f"- Provider: {response.provider}",
                f"- Model: {response.model}",
                f"- Route: {response.route_type}",
                "",
                response.text.strip(),
                "",
            ]
        )
    path.write_text(
        "\n".join(
            [
                f"# Transcript {assignment.assignment_id}",
                "",
                f"- Agent: {agent_id}",
                f"- Provider: {final_response.provider}",
                f"- Model: {final_response.model}",
                f"- Route: {final_response.route_type}",
                f"- Recorded At: {utc_now_iso()}",
                "",
                "## Assignment",
                assignment.assignment,
                "",
                "## Prompt",
                prompt,
                "",
                "## Tool Observations",
                json.dumps(observations, indent=2, sort_keys=True),
                "",
                *response_sections,
            ]
        ),
        encoding="utf-8",
    )
    return path


def _acquire_local_inference_lock(store: StateStore, agent_id: str) -> None:
    acquire = getattr(store, "acquire_local_inference_lock", None)
    if callable(acquire):
        acquire(agent_id, lock_ttl_seconds=LOCAL_INFERENCE_LOCK_TTL_SECONDS)
        return
    state = store.local_inference()
    next_available = state.get("next_available")
    if next_available and parse_utc_iso(next_available) > utc_now():
        raise RuntimeError(f"local inference unavailable until {next_available}")
    store.set_local_inference(
        {
            "status": "busy",
            "holder": agent_id,
            "last_completed": state.get("last_completed"),
            "next_available": next_available,
        }
    )


def _release_local_inference_lock(
    store: StateStore,
    agent_id: str,
    *,
    cooldown_seconds: int = LOCAL_INFERENCE_RELEASE_COOLDOWN_SECONDS,
) -> None:
    release = getattr(store, "release_local_inference_lock", None)
    if callable(release):
        release(agent_id, cooldown_seconds=cooldown_seconds)
        return
    state = store.local_inference()
    if state.get("holder") != agent_id:
        return
    completed_at = utc_now_iso()
    store.set_local_inference(
        {
            "status": "idle",
            "holder": None,
            "last_completed": completed_at,
            "next_available": add_seconds_iso(completed_at, cooldown_seconds),
        }
    )
