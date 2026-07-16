-- Release 1.1 chief chat: durable operator<->persona conversation threads.
-- Messages stay in brigade_chat_messages under channel 'thread:<id>'; this
-- table holds the thread identity, persona binding, and rolling summary.

create table if not exists brigade_conversations (
  id text primary key,
  operator_username text not null,
  persona text not null,
  chief_agent_id text,
  team_id text,
  status text not null default 'active',
  created_at timestamptz not null,
  updated_at timestamptz not null,
  record jsonb not null default '{}'::jsonb
);

create index if not exists brigade_conversations_operator_idx
  on brigade_conversations(operator_username);

-- One active thread per operator+persona: Telegram and the mobile SPA land in
-- the same conversation once identity resolves to the same username.
create unique index if not exists brigade_conversations_active_unique_idx
  on brigade_conversations(operator_username, persona)
  where status = 'active';
