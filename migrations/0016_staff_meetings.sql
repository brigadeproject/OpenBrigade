-- Durable Staff Meeting workflow state and append-only audit records.

create table if not exists brigade_staff_meeting_role_catalogs (
  version text primary key,
  active boolean not null default false,
  created_at timestamptz not null,
  record jsonb not null
);

create unique index if not exists brigade_staff_meeting_role_catalog_active_idx
  on brigade_staff_meeting_role_catalogs(active)
  where active = true;

create table if not exists brigade_staff_meetings (
  id text primary key,
  conversation_id text not null references brigade_conversations(id),
  owner_username text,
  team_id text,
  chair_agent_id text not null,
  status text not null,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  completed_at timestamptz,
  record jsonb not null
);

create index if not exists brigade_staff_meetings_status_idx
  on brigade_staff_meetings(status, updated_at desc);

create index if not exists brigade_staff_meetings_owner_idx
  on brigade_staff_meetings(owner_username, updated_at desc);

create index if not exists brigade_staff_meetings_team_idx
  on brigade_staff_meetings(team_id, updated_at desc);

-- Seats, packets, events, excerpts, vetoes, ballots, syntheses, and reports
-- share one append-only envelope. record_kind preserves their typed identity;
-- payload schemas are versioned by the Staff Meeting harness.
create table if not exists brigade_staff_meeting_records (
  id text primary key,
  meeting_id text not null references brigade_staff_meetings(id),
  conversation_id text not null references brigade_conversations(id),
  record_kind text not null,
  role_seat_id text,
  role_key text,
  round_number integer,
  phase text,
  assignment_id text,
  agent_id text,
  supersedes_record_id text references brigade_staff_meeting_records(id),
  idempotency_key text,
  created_at timestamptz not null,
  record jsonb not null
);

create index if not exists brigade_staff_meeting_records_meeting_idx
  on brigade_staff_meeting_records(meeting_id, created_at, id);

create index if not exists brigade_staff_meeting_records_kind_idx
  on brigade_staff_meeting_records(meeting_id, record_kind, created_at, id);

create index if not exists brigade_staff_meeting_records_assignment_idx
  on brigade_staff_meeting_records(assignment_id)
  where assignment_id is not null;

create unique index if not exists brigade_staff_meeting_records_idempotency_idx
  on brigade_staff_meeting_records(meeting_id, idempotency_key)
  where idempotency_key is not null;
