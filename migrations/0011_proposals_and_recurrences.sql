-- v1.0 orchestrator: pending proposals (efficiency, tool requests, rest insights)
-- and approved recurring-assignment templates.

create table if not exists brigade_proposals (
  id text primary key,
  kind text not null,
  status text not null default 'proposed',
  agent_id text,
  team_id text,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  idempotency_key text,
  record jsonb not null default '{}'::jsonb
);

create index if not exists brigade_proposals_status_idx
  on brigade_proposals(status);

create index if not exists brigade_proposals_kind_idx
  on brigade_proposals(kind);

create unique index if not exists brigade_proposals_idempotency_key_unique_idx
  on brigade_proposals(idempotency_key)
  where idempotency_key is not null;

create table if not exists brigade_recurrences (
  id text primary key,
  enabled boolean not null default true,
  interval_seconds integer not null,
  next_due_at timestamptz not null,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  record jsonb not null default '{}'::jsonb
);

create index if not exists brigade_recurrences_due_idx
  on brigade_recurrences(enabled, next_due_at);
