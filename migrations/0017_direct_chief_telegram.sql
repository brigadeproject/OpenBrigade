-- Opt-in named Telegram accounts and durable direct Crew Chief turns.

create table if not exists brigade_telegram_accounts (
  id text primary key,
  chief_agent_id text not null,
  enabled boolean not null default false,
  token_fingerprint text,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  record jsonb not null
);

create unique index if not exists brigade_telegram_accounts_enabled_chief_idx
  on brigade_telegram_accounts(chief_agent_id)
  where enabled = true;

create unique index if not exists brigade_telegram_accounts_token_idx
  on brigade_telegram_accounts(token_fingerprint)
  where token_fingerprint is not null;

create table if not exists brigade_chief_chat_policies (
  chief_agent_id text primary key,
  direct_enabled boolean not null default false,
  updated_at timestamptz not null,
  record jsonb not null
);

create table if not exists brigade_chief_interactive_turns (
  id text primary key,
  thread_id text not null references brigade_conversations(id),
  chief_agent_id text not null,
  operator_username text not null,
  status text not null,
  idempotency_key text,
  claim_owner text,
  claim_expires_at timestamptz,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  record jsonb not null
);

create unique index if not exists brigade_chief_turn_idempotency_idx
  on brigade_chief_interactive_turns(idempotency_key)
  where idempotency_key is not null;

create unique index if not exists brigade_chief_turn_active_thread_idx
  on brigade_chief_interactive_turns(thread_id)
  where status in ('queued', 'running', 'awaiting_permission');

create index if not exists brigade_chief_turn_status_idx
  on brigade_chief_interactive_turns(status, updated_at, id);
