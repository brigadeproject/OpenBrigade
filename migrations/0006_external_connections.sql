-- Durable external connector audit and identity approval records.

create table if not exists brigade_connector_audit_events (
  id text primary key,
  provider text not null,
  direction text not null,
  status text not null,
  external_user_id text,
  conversation_id text,
  external_message_id text,
  agent_id text,
  reason text,
  redacted_metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null,
  record jsonb not null default '{}'::jsonb
);

create index if not exists brigade_connector_audit_events_provider_idx
  on brigade_connector_audit_events(provider, created_at);

create index if not exists brigade_connector_audit_events_external_user_idx
  on brigade_connector_audit_events(provider, external_user_id);

create table if not exists brigade_external_identities (
  provider text not null,
  external_user_id text not null,
  username text,
  status text not null,
  reason text,
  redacted_metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  decided_at timestamptz,
  decided_by text,
  record jsonb not null default '{}'::jsonb,
  primary key (provider, external_user_id)
);

create index if not exists brigade_external_identities_status_idx
  on brigade_external_identities(status, updated_at);
