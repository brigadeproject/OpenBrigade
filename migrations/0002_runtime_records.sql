-- Runtime and record-oriented tables used by the live prototype repository layer.

alter table if exists brigade_missions
  add column if not exists record jsonb not null default '{}'::jsonb;

alter table if exists brigade_agents
  add column if not exists record jsonb not null default '{}'::jsonb;

alter table if exists brigade_users
  add column if not exists record jsonb not null default '{}'::jsonb;

alter table if exists brigade_goals
  add column if not exists record jsonb not null default '{}'::jsonb;

alter table if exists brigade_assignments
  add column if not exists progress_summary text,
  add column if not exists blockers jsonb not null default '[]'::jsonb,
  add column if not exists consecutive_failures integer not null default 0,
  add column if not exists last_error text,
  add column if not exists awaiting_human boolean not null default false,
  add column if not exists last_run_provider text,
  add column if not exists last_run_model text,
  add column if not exists last_run_at timestamptz,
  add column if not exists dependency_ids jsonb not null default '[]'::jsonb,
  add column if not exists goal_statement text,
  add column if not exists assignment_rationale text,
  add column if not exists created_by_user_id text,
  add column if not exists created_by_role text,
  add column if not exists idempotency_key text,
  add column if not exists record jsonb not null default '{}'::jsonb;

alter table if exists brigade_chat_messages
  add column if not exists metadata jsonb not null default '{}'::jsonb;

create table if not exists brigade_agent_states (
  agent_id text primary key references brigade_agents(id),
  record jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null
);

create table if not exists brigade_alerts (
  id text primary key,
  message text not null,
  created_at timestamptz not null
);

create table if not exists brigade_knowledge_chunks (
  id text primary key,
  document_id text not null,
  chunk_index integer not null,
  text text not null,
  source text,
  content_path text,
  created_at timestamptz not null,
  record jsonb not null default '{}'::jsonb
);

create index if not exists brigade_knowledge_chunks_document_idx
  on brigade_knowledge_chunks(document_id, chunk_index);

create table if not exists brigade_usage_records (
  id text primary key,
  assignment_id text,
  agent_id text,
  recorded_at timestamptz not null,
  record jsonb not null default '{}'::jsonb
);

create table if not exists brigade_cloud_jobs (
  id text primary key,
  assignment_id text,
  agent_id text,
  status text,
  updated_at timestamptz not null,
  record jsonb not null default '{}'::jsonb
);

create index if not exists brigade_cloud_jobs_status_idx
  on brigade_cloud_jobs(status);

create table if not exists brigade_financial_reports (
  id text primary key,
  generated_at timestamptz not null,
  record jsonb not null default '{}'::jsonb
);

create table if not exists brigade_local_inference_state (
  id text primary key,
  updated_at timestamptz not null,
  record jsonb not null default '{}'::jsonb
);

create table if not exists brigade_transcripts_runtime (
  id text primary key,
  assignment_id text,
  agent_id text,
  created_at timestamptz not null,
  record jsonb not null default '{}'::jsonb
);

create table if not exists brigade_episodes (
  id text primary key,
  agent_id text,
  created_at timestamptz not null,
  record jsonb not null default '{}'::jsonb
);

create table if not exists brigade_provenance_records (
  id text primary key,
  node_id text,
  node_type text,
  created_at timestamptz not null,
  record jsonb not null default '{}'::jsonb
);
