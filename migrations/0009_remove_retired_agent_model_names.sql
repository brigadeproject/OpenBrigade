-- Follow-up for databases that already applied 0008 before model_name='fake' was covered.

update brigade_agents
set record = jsonb_set(record, '{model_name}', '"gpt-oss:20b"'::jsonb, true)
where record->>'model_name' in ('fake', 'deterministic');
