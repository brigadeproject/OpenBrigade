# Repository Guidelines

## Project Structure & Module Organization
OpenBrigade is being built as a Python monorepo for proactive, orchestrator-driven agents. Treat `OpenBrigade_V0.1_Design_Summary.md` as the implementation source of truth and `OpenBrigade-Concept.md` as background context. The `reference/` directory contains reusable examples and prior art; adapt from it only after checking licenses and preserving attribution.

Future implementation should keep the Python package and CLI named `brigade`. Use explicit agent workspace manifests rather than globbing `workspace-*/`, because unrelated workspaces may exist on the same host. Per-agent workspaces should follow `workspace-{agent}/` and include files such as `AGENTS.md`, `IDENTITY.md`, `MEMORY.md`, `TOOLS.md`, `USER.md`, and `SOUL.md`.

## Build, Test, and Development Commands
There is no root application build yet. Useful current commands:

```bash
rg "orchestrator|goal|Redis" OpenBrigade*.md reference/
docker compose --env-file .env config
docker compose --env-file .env up -d
```

Once Python implementation begins, prefer `pytest` for tests, `ruff check .` for linting, and `ruff format .` for formatting.

## Coding Style & Naming Conventions
Use concise Markdown for specs and contributor docs. For Python, follow standard snake_case modules/functions, PascalCase classes, and typed interfaces where practical. Configuration should be JSON-first, with `brigade.config.json` for MVP config and `BRIGADE_*` environment variables for overrides. Store timestamps in UTC in datastores and emit structured JSON logs.

## Testing Guidelines
Add tests under `tests/` or next to the package being tested. Tests should run without live API keys, network access, or production services unless explicitly marked as integration tests. Cover orchestrator behavior around mission, goals, task state transitions, Redis runtime state, and PostgreSQL archival before expanding agent features.

## Commit & Pull Request Guidelines
Use clear imperative commits; Conventional Commit style is preferred, for example `docs: add contributor guide` or `infra: add brigade datastore stack`. PRs should describe purpose, changed modules or docs, validation performed, linked issues, and any license or attribution decisions for reference-derived work.

## Security & Configuration Tips
Do not commit real secrets. Start from `.env.example`, keep local `.env` private, and use the `brigade_` Docker stack so OpenBrigade services do not collide with production containers.
