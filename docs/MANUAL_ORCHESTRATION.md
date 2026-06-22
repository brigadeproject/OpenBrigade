# Manual Orchestration

Manual Orchestration is an operator-only administrative page for directly inspecting and managing orchestration tasks, agent queues, and task relationships.

This interface is intended for live operational override, recovery, and repair. All operator actions must be audited.

## Access

Manual Orchestration is available from the system dropdown menu, not as a standard workspace tab. Access is restricted to users with the operator or owner role.

The page opens separately from the main OpenBrigade interface while preserving the existing OpenBrigade visual design language.

## Purpose

The operator must be able to:

- inspect active, queued, blocked, and awaiting-completion tasks;
- view parent/child and dependency relationships;
- communicate with agents about specific task workflows;
- cancel, archive, requeue, reassign, reprioritize, or reissue tasks;
- repair task relationships;
- inspect task diagnostics and recent execution history;
- preserve audit history for all manual interventions.

## Layout

The page uses the standard OpenBrigade header with a single active section labeled “Manual Orchestration”.

The main workspace is a large relationship view showing tasks as a recursive tree. The tree should support left-to-right navigation, visible scrollbars, zooming, filtering, and node selection.

A selected task opens an inspector panel showing:

- task ID;
- task title/objective;
- current status;
- assigned agent;
- priority;
- queue position;
- parent task;
- child tasks;
- dependency relationships;
- blocking reason, if any;
- creation and update timestamps;
- latest heartbeat or execution status;
- recent logs or diagnostic summary;
- operator notes;
- available actions.

## Task Relationships

The interface must display both decomposition and dependency relationships.

Relationship types include:

- parent/child;
- depends on / blocks;
- awaiting completion;
- reissued from / supersedes;
- transferred from / transferred to;
- created by agent / created by operator.

Reissued tasks should not reuse the original task ID. Instead, the original task should be marked as superseded, cancelled, aborted, or failed, and the new task should record its lineage using `reissued_from_task_id`.

## Task Actions

### Talk to Agent

Opens a task-scoped chat with the assigned agent. Operator messages are attached to the selected task’s event history.

The operator may:

- provide clarification;
- answer agent questions;
- request a status update;
- add additional instructions;
- request a graceful stop;
- force-abort the current execution if necessary.

### Edit Task

Allows the operator to modify task instructions, acceptance criteria, priority, assignment, or notes.

Queued tasks may be edited directly. Running tasks should receive edits as operator intervention messages unless the task is first paused, aborted, or reissued.

### Cancel Task

#### Cancel and Archive

Stops the task, removes it from active execution or queue, and archives it as cancelled, failed, or aborted. The operator may include a note. Notes should be required for running tasks, blocked tasks, or tasks with active children.

#### Remove Unstarted Task

Deletes or hides a queued task that has never started. A tombstone audit record should still be preserved.

Already-started tasks should not be hard-deleted.

### Requeue / Reprioritize

Allows the operator to move an unstarted or paused task within the current queue or change its priority.

Options:

- move to end of current queue;
- move earlier/later;
- set explicit priority;
- schedule for later execution.

### Reissue Task

Ends the current task and creates a new supersceeding task attempt from the same prompt and metadata.

The new task receives a new task ID and records:

- original task ID;
- parent task ID;
- root task ID;
- copied prompt;
- copied or updated priority;
- copied or updated assignment;
- operator note;
- attempt number.

### Reassign Task

Transfers the task to another agent.

For queued tasks, reassignment updates the queue and agent worklist.

For running tasks, the operator must choose whether to:

- request graceful stop and transfer;
- force abort and transfer;
- clone to another agent while leaving the current task running.

The operator chooses the destination agent, priority, and optional notes.

### Relationship Repair

The operator may:

- attach an orphan task to a parent;
- detach a task from a parent;
- change a task’s parent;
- add or remove dependencies;
- rebuild awaiting-completion links;
- cancel a parent while preserving, cancelling, or reparenting children.

## Safety Rules

Before destructive actions, the interface must show a confirmation dialog with:

- task ID;
- task title;
- current status;
- assigned agent;
- affected parent task;
- affected child tasks;
- affected dependencies;
- whether the task is currently running;
- whether execution history will be preserved;
- required or optional operator note.

Actions affecting task trees must allow the operator to choose whether descendants are cancelled, preserved, detached, or reparented.

## Audit Logging

Every manual intervention must produce an audit event containing:

- operator ID;
- timestamp;
- action type;
- task ID;
- previous state;
- new state;
- previous assignment;
- new assignment;
- previous priority;
- new priority;
- affected relationships;
- affected child tasks;
- operator note.

Audit records should be stored in the orchestrator’s durable history tables.

## HEARTBEAT.md Synchronization

The orchestrator remains the canonical source for task state. When operator actions modify task assignment or queue state, the orchestrator must synchronize the affected agent `HEARTBEAT.md` files so existing OpenClaw heartbeat behavior continues to work.

If synchronization fails, the UI must show the task as `sync_failed` or `sync_pending` and provide diagnostics.

## Diagnostics

For each selected task, the interface should show current execution diagnostics using OpenGauge first, with OpenClaw logs as fallback.

Diagnostics may include:

- latest heartbeat;
- current agent status;
- current execution phase;
- recent errors;
- recent log excerpt;
- retry count;
- blocked reason;
- last update time.

-----
### Bottom Line
- Do not reuse task IDs for reissued tasks.
- Do not hard-delete started tasks.
- Treat relationship repair as a first-class feature.
- Add audit logging as mandatory, not optional.
- Distinguish queued-task edits from running-task interventions.
- Add blocked-task handling.

The interface should feel simple, but the commands need to be explicit.
