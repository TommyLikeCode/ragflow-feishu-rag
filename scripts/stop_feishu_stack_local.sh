#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT_DIR/logs"
TASK_PID_FILE="$LOG_DIR/feishu_task_executor_local.pid"
SERVER_PID_FILE="$LOG_DIR/feishu_server_local.pid"
SIDECAR_PID_FILE="$LOG_DIR/feishu_sidecar_local.pid"

log() {
  printf '[feishu-stack-stop] %s\n' "$*"
}

kill_from_pid_file() {
  local pid_file=$1
  local label=$2

  if [[ ! -f "$pid_file" ]]; then
    return 0
  fi

  local pid
  pid=$(cat "$pid_file" 2>/dev/null || true)
  rm -f "$pid_file"

  if [[ -z "$pid" ]]; then
    return 0
  fi

  if kill -0 "$pid" 2>/dev/null; then
    log "Stopping $label pid=$pid"
    kill "$pid" 2>/dev/null || true
  else
    log "$label pid=$pid is already stopped"
  fi
}

main() {
  kill_from_pid_file "$SIDECAR_PID_FILE" "sidecar"
  kill_from_pid_file "$SERVER_PID_FILE" "ragflow server"
  kill_from_pid_file "$TASK_PID_FILE" "task executor"

  pkill -f "scripts/run_feishu_ws.py" 2>/dev/null || true
  pkill -f "api/ragflow_server.py" 2>/dev/null || true
  pkill -f "rag/svr/task_executor.py" 2>/dev/null || true

  log "Stop signal sent"
}

main "$@"
