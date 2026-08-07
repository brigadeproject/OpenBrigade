create table if not exists brigade_policy_projections (
  id text primary key,
  agent_id text not null references brigade_agents(id) on delete cascade,
  path text not null,
  file_kind text not null,
  content_hash text not null,
  parsed_version integer not null,
  status text not null,
  updated_at timestamptz not null,
  record jsonb not null default '{}'::jsonb,
  unique (agent_id, path)
);

create index if not exists brigade_policy_projections_agent_idx
  on brigade_policy_projections(agent_id, path);
