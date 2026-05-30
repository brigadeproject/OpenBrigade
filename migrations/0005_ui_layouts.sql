create table if not exists brigade_ui_layouts (
  user_key text not null,
  layout_key text not null,
  updated_at timestamptz not null,
  record jsonb not null default '{}'::jsonb,
  primary key (user_key, layout_key)
);
