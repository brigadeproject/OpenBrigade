# OpenBrigade Dedicated Server

Dedicated Server mode runs the web gateway and orchestrator from
`/opt/openbrigade` as the `brigade` service account. PostgreSQL, Redis, Qdrant,
Neo4j, SearXNG, and the isolated browser worker stay in Compose with persistent
data below `/srv/openbrigade/services`.

## 1. Administrator preparation

Install Docker Compose, Node/npm, and Python 3.10 or newer. Create the `brigade`
account, clone the intended release at `/opt/openbrigade`, and run:

```bash
sudo BRIGADE_SERVICE_USER=brigade ./deploy/install-dedicated-system.sh
sudo -u brigade python3 -m venv .venv
sudo -u brigade .venv/bin/pip install -e ".[dev,web,models,ingest]"
cd web && sudo -u brigade npm ci && sudo -u brigade npm run build
```

Copy `deploy/dedicated.env.example` to `/opt/openbrigade/.env`, replace every
placeholder secret without placing it in shell history, then set ownership to
`root:brigade` and mode `0640`. `BRIGADE_REQUIRE_AUTH=true` and loopback web
binding are required defaults. Any LAN binding is a deliberate firewall-scoped
override, never a broad public bind.

## 2. Start support services

```bash
docker compose -f compose.dedicated.yml --env-file .env config
docker compose -f compose.dedicated.yml --env-file .env up -d --build
.venv/bin/brigade db migrate
.venv/bin/brigade health --json
```

Confirm that both inference URLs respond independently and that the embedding
model dimension matches `BRIGADE_OLLAMA_EMBEDDING_VECTOR_SIZE`. The default
template uses separate generation (`:11434`) and embedding (`:11435`) endpoints.

## 3. Start and verify host services

The installer copies unit files into `/etc/systemd/system`; do not replace them
with symlinks into `/opt`, which may be unavailable while systemd builds the boot
transaction.

```bash
sudo systemctl enable --now openbrigade-web openbrigade-orchestrator
systemctl status openbrigade-web openbrigade-orchestrator
curl --fail http://127.0.0.1:58080/healthz
.venv/bin/brigade health --json
```

Create the first owner, run one bounded team assignment, and verify transcript
persistence under `/srv/openbrigade/app`. Restart the support stack, then reboot
once before accepting the server. Re-check services, migrations, health, model
generation, embeddings, and authentication after boot.

## Optional direct Crew Chief bots and maintenance

Dedicated Server mode supports opt-in one-to-one Telegram bots for Crew Chiefs.
Create and test each bot while disabled, enable its explicit direct-chat policy,
then enable polling. The protected token files live below
`/srv/openbrigade/app/secrets/connectors/telegram` by default and must be part of
the protected backup. Postgres and Redis must both be healthy.

Infrastructure maintenance remains an operator-defined allowlist, not an open
privileged shell. Copy `docs/maintenance-actions.example.json` to a root-owned
path outside the checkout, for example
`/etc/openbrigade/maintenance-actions.json`, set `root:brigade` ownership and
mode `0640`, then configure `BRIGADE_MAINTENANCE_ACTIONS_PATH` in the protected
environment file. Entries contain exact argv and allowed Chief IDs. If an entry
uses `sudo`, the existing host sudoers policy must already authorize that exact
non-interactive command; OpenBrigade never writes or bypasses sudoers.

After changing the environment, restart both host services. Account and policy
changes made through the UI or CLI are reconciled by the running orchestrator
without a restart. During an OpenClaw migration, stop its poller only after the
new account tests successfully, retain the old service intact for rollback, and
never allow both systems to poll the same bot token.

## Recovery and backup

Back up `/srv/openbrigade` and the protected `.env` through the host's approved
backup mechanism. Do not use wipe helpers on this layout unless their targets have
been explicitly audited. Logs are available through `journalctl -u
openbrigade-web` and `journalctl -u openbrigade-orchestrator`.
