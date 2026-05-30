#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BACKUP_DIR="${1:-}"
ENV_FILE="${ENV_FILE:-.env}"

if [[ -z "$BACKUP_DIR" ]]; then
  echo "usage: $0 <backup-dir>" >&2
  exit 2
fi

if [[ ! -d "$BACKUP_DIR" ]]; then
  echo "missing backup dir: $BACKUP_DIR" >&2
  exit 1
fi

if [[ ! -f "$BACKUP_DIR/compose.resolved.yaml" ]]; then
  echo "backup dir is missing compose.resolved.yaml" >&2
  exit 1
fi

docker compose --env-file "$ENV_FILE" --profile app down --remove-orphans

restore_volume() {
  local volume_name="$1"
  local archive_path="$2"
  docker volume create "$volume_name" >/dev/null
  docker run --rm \
    -v "$volume_name:/target" \
    -v "$BACKUP_DIR:/backup:ro" \
    alpine:3.20 \
    sh -lc "find /target -mindepth 1 -maxdepth 1 -exec rm -rf {} +; tar -C /target -xzf /backup/$archive_path"
}

restore_volume brigade_app_data brigade_app_data.tar.gz
restore_volume brigade_postgres_data brigade_postgres_data.tar.gz
restore_volume brigade_redis_data brigade_redis_data.tar.gz
restore_volume brigade_qdrant_data brigade_qdrant_data.tar.gz
restore_volume brigade_neo4j_data brigade_neo4j_data.tar.gz
restore_volume brigade_neo4j_logs brigade_neo4j_logs.tar.gz

docker compose --env-file "$ENV_FILE" --profile app up -d --build
docker compose --env-file "$ENV_FILE" --profile app ps
