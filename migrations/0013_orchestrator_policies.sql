-- Durable orchestrator directives set via Orchestrator Chat: structured routing
-- rules the ladder/orchestrator consult mechanically, plus freeform standing
-- instructions injected into orchestrator prompts.

create table if not exists brigade_orchestrator_policies (
  id text primary key,
  rule_kind text not null default 'freeform',
  assignment_kind text,
  active boolean not null default true,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  record jsonb not null default '{}'::jsonb
);

create index if not exists brigade_orchestrator_policies_active_idx
  on brigade_orchestrator_policies(active);

create index if not exists brigade_orchestrator_policies_assignment_kind_idx
  on brigade_orchestrator_policies(assignment_kind);
