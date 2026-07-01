-- Cache provider-probed model inventories for API/UI use.

create table if not exists brigade_model_inventory (
  provider text primary key,
  probed_at timestamptz not null,
  status text not null,
  record jsonb not null
);

create index if not exists brigade_model_inventory_status_idx
  on brigade_model_inventory(status);
