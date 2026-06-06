-- Prevent duplicate active assignments from repeated client submissions or daemon ticks.

create unique index if not exists brigade_assignments_idempotency_key_unique_idx
  on brigade_assignments(idempotency_key)
  where idempotency_key is not null;
