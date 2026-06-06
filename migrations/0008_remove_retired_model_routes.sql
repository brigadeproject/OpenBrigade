-- Remove retired test-model route values from persisted agent records.

update brigade_agents
set record = jsonb_set(record, '{model_provider}', '"ollama"'::jsonb, true)
where record->>'model_provider' = 'fake';

update brigade_agents
set record = jsonb_set(record, '{model_name}', '"gpt-oss:20b"'::jsonb, true)
where record->>'model_name' in ('fake', 'deterministic');
