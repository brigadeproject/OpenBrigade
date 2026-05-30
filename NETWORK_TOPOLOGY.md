# OpenBrigade Network Topology

This document describes the expected local network shape for v0.9.

## Compose Services

The root `docker-compose.yml` defines the live stack under the `app` profile:

- `brigade_web` listens on container port `8080` and is published as
  `${BRIGADE_BIND_ADDRESS:-127.0.0.1}:${BRIGADE_WEB_PORT:-58080}`.
- `brigade_orchestrator` runs the background orchestration daemon.
- `brigade_postgres` listens on container port `5432`, published by default on host port `55432`.
- `brigade_redis` listens on container port `6379`, published by default on host port `56379`.
- `brigade_qdrant` listens on container ports `6333` and `6334`, published by default on host ports
  `56333` and `56334`.
- `brigade_neo4j` listens on container ports `7474` and `7687`, published by default on host ports
  `57474` and `57687`.
- `brigade_ollama_proxy` uses host networking so containers can reach the host Ollama service.

All app/datastore services except the Ollama proxy share the `brigade_net` Docker network.

## Request Paths

CLI live command:

```text
operator shell -> ./ops/brigade-live.sh -> docker exec brigade_orchestrator -> brigade CLI
```

Web request:

```text
browser -> brigade_web:8080 -> RBAC -> Postgres -> Redis/Qdrant/Neo4j as needed
```

Orchestrator assignment:

```text
brigade_orchestrator -> Postgres assignments/goals -> Redis runtime queue/claims -> workspace HEARTBEAT.md
```

Local model call:

```text
runner -> Redis local inference lock -> brigade_ollama_proxy -> host Ollama -> runner records result
```

External provider calls are v0.9.1 work and should be disabled unless their credentials and
allowlists are deliberately configured.

## Binding and Auth

For local-only operation, keep `BRIGADE_BIND_ADDRESS=127.0.0.1`. Binding to `0.0.0.0` is useful for
LAN testing but exposes the web gateway and datastore ports to any host that can reach the machine.

When `BRIGADE_REQUIRE_AUTH=false`, `brigade_web` warns if it binds to a reachable host. For any
shared network beyond a trusted LAN, set:

```env
BRIGADE_REQUIRE_AUTH=true
BRIGADE_JWT_SECRET=<strong-secret>
```

Then issue a token through an owner account and use that token in the web UI.

## Health Checks

Use these checks after rebuilds, rebinds, and recovery runs:

```bash
docker compose --env-file .env --profile app ps
./ops/brigade-live.sh health --json
./ops/brigade-live.sh db status
./ops/brigade-live.sh datastore inspect --backend redis
./ops/brigade-live.sh datastore inspect --backend qdrant
./ops/brigade-live.sh datastore inspect --backend neo4j
```

The web UI additionally needs `/`, `/healthz`, `/api/ops-room`, and bundled
`/assets/pixel-agents/...` files to return successfully from the exposed web port.
