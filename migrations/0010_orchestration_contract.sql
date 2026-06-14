-- v1.0 orchestrator contract: assignment kinds, goal engagement modes, agent specialties.

alter table if exists brigade_assignments
  add column if not exists kind text not null default 'mission';

create index if not exists brigade_assignments_kind_idx
  on brigade_assignments(kind);

alter table if exists brigade_goals
  add column if not exists engagement_mode text not null default 'directive';

alter table if exists brigade_agents
  add column if not exists specialties jsonb not null default '[]'::jsonb;
