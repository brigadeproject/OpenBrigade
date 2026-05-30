#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ACTION="${1:-create}"
SESSION_NAME="${2:-${BRIGADE_TEST_SESSION:-brigade-test}}"
ENV_FILE="${ENV_FILE:-.env}"
LOG_SERVICES="${LOG_SERVICES:-brigade_orchestrator}"
LOG_TAIL="${LOG_TAIL:-150}"
POLL_SECONDS="${POLL_SECONDS:-10}"

usage() {
  cat <<EOF
usage: $0 [create|attach|kill|print] [session-name]

environment:
  BRIGADE_TEST_SESSION  default session name
  ENV_FILE              compose env file, default .env
  LOG_SERVICES          docker compose services for the log pane
  LOG_TAIL              number of log lines to preload, default 150
  POLL_SECONDS          watch-pane refresh interval, default 10
EOF
}

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is required for shared test sessions" >&2
  exit 1
fi

case "$ACTION" in
  create)
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
      echo "session already exists: $SESSION_NAME"
      echo "attach with: tmux attach -t $SESSION_NAME"
      exit 0
    fi

    tmux new-session -d -s "$SESSION_NAME" -c "$ROOT_DIR"
    tmux rename-window -t "$SESSION_NAME:0" "prototype"
    tmux set-option -t "$SESSION_NAME" mouse on
    tmux set-option -t "$SESSION_NAME" history-limit 50000
    tmux setw -t "$SESSION_NAME:0" remain-on-exit on

    tmux send-keys -t "$SESSION_NAME:0.0" \
      "printf '%s\n' 'OpenBrigade shared prototype test session' 'Pane 0: interactive test commands' 'Pane 1: live container logs' 'Pane 2: watch loop' 'Pane 3: preflight snapshot' '' 'Suggested start:' './ops/prototype-preflight.sh' 'Attach from another shell with: tmux attach -t $SESSION_NAME'" \
      C-m

    tmux split-window -h -t "$SESSION_NAME:0" -c "$ROOT_DIR"
    tmux send-keys -t "$SESSION_NAME:0.1" \
      "docker compose --env-file \"$ENV_FILE\" --profile app logs -f --tail=$LOG_TAIL $LOG_SERVICES" \
      C-m

    tmux split-window -v -t "$SESSION_NAME:0.0" -c "$ROOT_DIR"
    tmux send-keys -t "$SESSION_NAME:0.2" \
      "POLL_SECONDS=$POLL_SECONDS ./ops/prototype-watch.sh" \
      C-m

    tmux split-window -v -t "$SESSION_NAME:0.1" -c "$ROOT_DIR"
    tmux send-keys -t "$SESSION_NAME:0.3" "./ops/prototype-preflight.sh" C-m

    tmux select-layout -t "$SESSION_NAME:0" main-vertical
    tmux select-pane -t "$SESSION_NAME:0.0"

    echo "created session: $SESSION_NAME"
    echo "attach with: tmux attach -t $SESSION_NAME"
    ;;
  attach)
    exec tmux attach -t "$SESSION_NAME"
    ;;
  kill)
    tmux kill-session -t "$SESSION_NAME"
    ;;
  print)
    cat <<EOF
session=$SESSION_NAME
root=$ROOT_DIR
env_file=$ENV_FILE
log_services=$LOG_SERVICES
poll_seconds=$POLL_SECONDS
attach_command=tmux attach -t $SESSION_NAME
EOF
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
