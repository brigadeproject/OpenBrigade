create table if not exists brigade_teams (
  id text primary key,
  display_name text not null,
  description text,
  parent_team_id text references brigade_teams(id),
  crew_chief_id text references brigade_agents(id),
  members jsonb not null default '[]'::jsonb,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  record jsonb not null default '{}'::jsonb
);
