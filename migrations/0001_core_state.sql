-- Core OpenBrigade state schema.
-- Apply with the eventual Alembic migration wrapper; kept SQL-first for the initial scaffold.

create table if not exists brigade_missions (
  id text primary key,
  statement text not null,
  success_criteria jsonb not null default '[]'::jsonb,
  explicitly_not jsonb not null default '[]'::jsonb,
  set_at timestamptz not null,
  last_reviewed timestamptz not null
);

create table if not exists brigade_agents (
  id text primary key,
  display_name text not null,
  workspace_path text not null,
  role text not null default 'line_worker',
  status text not null default 'idle',
  created_at timestamptz not null
);

create table if not exists brigade_users (
  username text primary key,
  role text not null,
  created_at timestamptz not null
);

create table if not exists brigade_goals (
  id text primary key,
  agent_id text not null references brigade_agents(id),
  statement text not null,
  success_criteria jsonb not null default '[]'::jsonb,
  explicitly_not jsonb not null default '[]'::jsonb,
  set_by text not null,
  human_confirmed boolean not null default false,
  set_at timestamptz not null
);

create table if not exists brigade_assignments (
  id text primary key,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  created_by text not null,
  assigned_to text not null references brigade_agents(id),
  source text not null,
  assignment text not null,
  work_mode text not null,
  status text not null,
  priority text not null,
  estimated_cycles integer not null default 1,
  cycle_count integer not null default 0,
  checkpoint_at timestamptz,
  parent_assignment_id text references brigade_assignments(id),
  result_artifact_ids jsonb not null default '[]'::jsonb,
  transcript_path text,
  state_row_written_to text
);

create index if not exists brigade_assignments_status_idx
  on brigade_assignments(status);

create index if not exists brigade_assignments_assigned_to_idx
  on brigade_assignments(assigned_to);

create table if not exists brigade_assignment_history (
  id text primary key,
  assignment_id text not null,
  archived_at timestamptz not null,
  final_status text not null,
  executive_summary text,
  failure_info text,
  record jsonb not null
);

create table if not exists brigade_orchestrator_reasoning (
  id text primary key,
  cycle_at timestamptz not null,
  mission_id text references brigade_missions(id),
  reasoning jsonb not null,
  decisions jsonb not null default '[]'::jsonb
);

create table if not exists brigade_dispatch_transcripts (
  id text primary key,
  assignment_id text not null,
  agent_id text not null references brigade_agents(id),
  transcript_path text not null,
  created_at timestamptz not null,
  metadata jsonb not null default '{}'::jsonb
);

create table if not exists brigade_knowledge_documents (
  id text primary key,
  title text not null,
  source text not null,
  document_type text not null,
  content_path text not null,
  ingested_at timestamptz not null,
  metadata jsonb not null default '{}'::jsonb
);

create table if not exists brigade_chat_messages (
  id text primary key,
  channel text not null,
  sender text not null,
  recipient text not null,
  content text not null,
  created_at timestamptz not null
);

create index if not exists brigade_chat_messages_channel_idx
  on brigade_chat_messages(channel);
