#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

CONTAINER="${BRIGADE_LIVE_CONTAINER:-brigade_orchestrator}"
DOCKER_EXEC=(docker exec)
if [[ -t 0 && -t 1 ]]; then
  DOCKER_EXEC+=(-it)
fi

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "live brigade container not running: $CONTAINER" >&2
  echo "start it with: docker compose --env-file .env --profile app up -d --build" >&2
  exit 1
fi

if [[ "${1:-}" == "knowledge" && ( "${2:-}" == "upload" || "${2:-}" == "ingest" ) ]]; then
  args=("$@")
  for ((i = 0; i < ${#args[@]}; i++)); do
    if [[ "${args[$i]}" == "--path" && $((i + 1)) -lt ${#args[@]} ]]; then
      host_path="${args[$((i + 1))]}"
      if [[ -f "$host_path" ]]; then
        upload_dir="/tmp/openbrigade-uploads/$$"
        upload_path="$upload_dir/$(basename "$host_path")"
        docker exec "$CONTAINER" mkdir -p "$upload_dir"
        docker cp "$host_path" "$CONTAINER:$upload_path"
        args[$((i + 1))]="$upload_path"
      fi
      break
    fi
  done
  exec "${DOCKER_EXEC[@]}" "$CONTAINER" brigade "${args[@]}"
fi

exec "${DOCKER_EXEC[@]}" "$CONTAINER" brigade "$@"
