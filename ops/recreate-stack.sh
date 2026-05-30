#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE="${ENV_FILE:-.env}"
DROP_VOLUMES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --drop-volumes)
      DROP_VOLUMES=1
      shift
      ;;
    *)
      echo "unknown option: $1" >&2
      echo "usage: $0 [--drop-volumes]" >&2
      exit 2
      ;;
  esac
done

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing env file: $ENV_FILE" >&2
  exit 1
fi

if [[ "$DROP_VOLUMES" -eq 1 ]]; then
  docker compose --env-file "$ENV_FILE" --profile app down -v --remove-orphans
else
  docker compose --env-file "$ENV_FILE" --profile app down --remove-orphans
fi

docker compose --env-file "$ENV_FILE" --profile app up -d --build --force-recreate
docker compose --env-file "$ENV_FILE" --profile app ps
