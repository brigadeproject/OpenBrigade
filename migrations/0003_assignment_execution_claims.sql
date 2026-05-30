create table if not exists brigade_assignment_execution_claims (
  assignment_id text primary key references brigade_assignments(id) on delete cascade,
  agent_id text not null references brigade_agents(id),
  run_owner text not null,
  claimed_at timestamptz not null
);

create index if not exists brigade_assignment_execution_claims_agent_idx
  on brigade_assignment_execution_claims(agent_id);
