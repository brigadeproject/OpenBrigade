#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_ROOT="${BACKUP_ROOT:-$ROOT_DIR/backups}"
BACKUP_DIR="${1:-$BACKUP_ROOT/$TIMESTAMP}"
ENV_FILE="${ENV_FILE:-.env}"

mkdir -p "$BACKUP_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing env file: $ENV_FILE" >&2
  exit 1
fi

docker compose --env-file "$ENV_FILE" --profile app config > "$BACKUP_DIR/compose.resolved.yaml"
cp "$ENV_FILE" "$BACKUP_DIR/.env.snapshot"
cp docker-compose.yml "$BACKUP_DIR/docker-compose.yml"
cp BACKUP.md "$BACKUP_DIR/BACKUP.md"

tar \
  --exclude='reference' \
  --exclude='backups' \
  --exclude='.brigade' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  --exclude='.ruff_cache' \
  --exclude='web/node_modules' \
  --exclude='web/dist' \
  --exclude='.git' \
  -czf "$BACKUP_DIR/openbrigade-source.tar.gz" \
  .

if docker ps --format '{{.Names}}' | grep -qx brigade_orchestrator; then
  docker exec brigade_orchestrator sh -lc \
    'tar -C /data -czf /tmp/brigade-app-data.tar.gz .'
  docker cp brigade_orchestrator:/tmp/brigade-app-data.tar.gz \
    "$BACKUP_DIR/brigade-app-data.tar.gz"
  docker exec brigade_orchestrator rm -f /tmp/brigade-app-data.tar.gz
fi

if docker ps --format '{{.Names}}' | grep -qx brigade_postgres; then
  docker exec brigade_postgres sh -lc \
    'export PGPASSWORD="$POSTGRES_PASSWORD"; pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f /tmp/brigade-postgres.dump'
  docker cp brigade_postgres:/tmp/brigade-postgres.dump \
    "$BACKUP_DIR/brigade-postgres.dump"
  docker exec brigade_postgres rm -f /tmp/brigade-postgres.dump
fi

snapshot_volume() {
  local volume_name="$1"
  local archive_name="$2"
  docker run --rm \
    -v "$volume_name:/source:ro" \
    -v "$BACKUP_DIR:/backup" \
    alpine:3.20 \
    sh -lc "tar -C /source -czf /backup/$archive_name ."
}

for volume in \
  brigade_app_data \
  brigade_postgres_data \
  brigade_redis_data \
  brigade_qdrant_data \
  brigade_neo4j_data \
  brigade_neo4j_logs
do
  snapshot_volume "$volume" "$volume.tar.gz"
done

cat > "$BACKUP_DIR/manifest.txt" <<EOF
timestamp=$TIMESTAMP
env_file=$ENV_FILE
compose_project=brigade
saved_items=
  compose.resolved.yaml
  .env.snapshot
  docker-compose.yml
  BACKUP.md
  openbrigade-source.tar.gz
  brigade-app-data.tar.gz
  brigade-postgres.dump
  brigade_app_data.tar.gz
  brigade_postgres_data.tar.gz
  brigade_redis_data.tar.gz
  brigade_qdrant_data.tar.gz
  brigade_neo4j_data.tar.gz
  brigade_neo4j_logs.tar.gz
EOF

echo "$BACKUP_DIR"
