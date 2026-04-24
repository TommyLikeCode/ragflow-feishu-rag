#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${FEISHU_ENV_FILE:-$ROOT_DIR/.env.feishu.local}"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
PYTHONPATH_VALUE="${PYTHONPATH_VALUE:-$ROOT_DIR}"
READY_TIMEOUT_SECONDS="${READY_TIMEOUT_SECONDS:-300}"
TASK_EXECUTOR_ID="${TASK_EXECUTOR_ID:-1}"
LOG_DIR="$ROOT_DIR/logs"
BACKEND_LOG="$LOG_DIR/feishu_backend_local.log"
SIDECAR_LOG="$LOG_DIR/feishu_sidecar_local.log"
TASK_PID_FILE="$LOG_DIR/feishu_task_executor_local.pid"
SERVER_PID_FILE="$LOG_DIR/feishu_server_local.pid"
SIDECAR_PID_FILE="$LOG_DIR/feishu_sidecar_local.pid"
TASK_PID=""
SERVER_PID=""
SIDECAR_PID=""

log() {
  printf '[feishu-stack] %s\n' "$*"
}

fail() {
  log "ERROR: $*"
  exit 1
}

cleanup() {
  local exit_code=$?

  if [[ -n "${SIDECAR_PID:-}" ]] && kill -0 "$SIDECAR_PID" 2>/dev/null; then
    log "Stopping sidecar pid=$SIDECAR_PID"
    kill "$SIDECAR_PID" 2>/dev/null || true
    wait "$SIDECAR_PID" 2>/dev/null || true
  fi

  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    log "Stopping ragflow server pid=$SERVER_PID"
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi

  if [[ -n "${TASK_PID:-}" ]] && kill -0 "$TASK_PID" 2>/dev/null; then
    log "Stopping task executor pid=$TASK_PID"
    kill "$TASK_PID" 2>/dev/null || true
    wait "$TASK_PID" 2>/dev/null || true
  fi

  rm -f "$TASK_PID_FILE" "$SERVER_PID_FILE" "$SIDECAR_PID_FILE"
  exit "$exit_code"
}

trap cleanup EXIT INT TERM

require_file() {
  local path=$1
  [[ -f "$path" ]] || fail "Required file not found: $path"
}

load_env() {
  require_file "$ENV_FILE"
  log "Loading env from $ENV_FILE"
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
}

require_env() {
  local name=$1
  local value="${!name:-}"
  if [[ -z "$value" || "$value" == "__REPLACE_ME__" ]]; then
    fail "Environment variable $name is missing or still set to __REPLACE_ME__ in $ENV_FILE"
  fi
}

ensure_not_running() {
  local pid_file=$1
  local label=$2
  if [[ -f "$pid_file" ]]; then
    local existing_pid
    existing_pid=$(cat "$pid_file" 2>/dev/null || true)
    if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
      fail "$label already appears to be running with pid=$existing_pid"
    fi
    rm -f "$pid_file"
  fi
}

wait_for_port() {
  "$PYTHON_BIN" - <<'PY'
import socket
sock = socket.socket()
sock.settimeout(1)
try:
    sock.connect(("127.0.0.1", 9380))
finally:
    sock.close()
PY
}

wait_for_backend_ready() {
  local start_ts
  local elapsed
  local last_log_second=-1
  local port_ready=0
  start_ts=$(date +%s)
  log "Waiting for backend readiness (timeout=${READY_TIMEOUT_SECONDS}s): stage1=port 9380, stage2=/api/v1/feishu/ping"

  while true; do
    elapsed=$(( $(date +%s) - start_ts ))

    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
      log "ragflow server exited unexpectedly. Recent backend log:"
      tail -n 80 "$BACKEND_LOG" || true
      fail "ragflow server failed before becoming ready"
    fi

    if ! kill -0 "$TASK_PID" 2>/dev/null; then
      log "task executor exited unexpectedly. Recent backend log:"
      tail -n 80 "$BACKEND_LOG" || true
      fail "task executor failed before backend became ready"
    fi

    if (( port_ready == 0 )); then
      if wait_for_port >/dev/null 2>&1; then
        port_ready=1
        log "Stage1 ready: port 9380 is listening; now waiting for /api/v1/feishu/ping"
      elif (( elapsed == 0 || elapsed - last_log_second >= 10 )); then
        log "Stage1 waiting: port 9380 not listening yet (elapsed=${elapsed}s)"
        last_log_second=$elapsed
      fi
    else
      if curl --silent --show-error --fail --max-time 3 \
        http://127.0.0.1:9380/api/v1/feishu/ping >/dev/null; then
        log "Stage2 ready: /api/v1/feishu/ping healthy (elapsed=${elapsed}s)"
        log "Backend is ready on port 9380 and /api/v1/feishu/ping is healthy"
        return 0
      elif (( elapsed == 0 || elapsed - last_log_second >= 10 )); then
        log "Stage2 waiting: port is up, but /api/v1/feishu/ping not healthy yet (elapsed=${elapsed}s)"
        last_log_second=$elapsed
      fi
    fi

    if (( elapsed >= READY_TIMEOUT_SECONDS )); then
      log "Timed out waiting for backend readiness. Recent backend log:"
      tail -n 80 "$BACKEND_LOG" || true
      fail "Backend did not become ready within ${READY_TIMEOUT_SECONDS}s"
    fi

    sleep 1
  done
}

start_task_executor() {
  ensure_not_running "$TASK_PID_FILE" "task executor"
  log "Starting task executor id=$TASK_EXECUTOR_ID"
  (
    cd "$ROOT_DIR"
    export PYTHONPATH="$PYTHONPATH_VALUE"
    export NLTK_DATA="$ROOT_DIR/nltk_data"
    if command -v pkg-config >/dev/null 2>&1 && pkg-config --exists jemalloc; then
      JEMALLOC_PATH="$(pkg-config --variable=libdir jemalloc)/libjemalloc.so"
      export LD_PRELOAD="$JEMALLOC_PATH"
    fi
    exec "$PYTHON_BIN" rag/svr/task_executor.py "$TASK_EXECUTOR_ID"
  ) >>"$BACKEND_LOG" 2>&1 &
  TASK_PID=$!
  printf '%s\n' "$TASK_PID" > "$TASK_PID_FILE"
  log "task executor pid=$TASK_PID"
}

start_server() {
  ensure_not_running "$SERVER_PID_FILE" "ragflow server"
  log "Starting ragflow server"
  (
    cd "$ROOT_DIR"
    export PYTHONPATH="$PYTHONPATH_VALUE"
    export NLTK_DATA="$ROOT_DIR/nltk_data"
    exec "$PYTHON_BIN" api/ragflow_server.py
  ) >>"$BACKEND_LOG" 2>&1 &
  SERVER_PID=$!
  printf '%s\n' "$SERVER_PID" > "$SERVER_PID_FILE"
  log "ragflow server pid=$SERVER_PID (log: $BACKEND_LOG)"
}

start_sidecar() {
  ensure_not_running "$SIDECAR_PID_FILE" "sidecar"
  log "Starting Feishu sidecar in foreground log mode"
  (
    cd "$ROOT_DIR"
    export PYTHONPATH="$PYTHONPATH_VALUE"
    exec "$PYTHON_BIN" scripts/run_feishu_ws.py
  ) 2>&1 | tee -a "$SIDECAR_LOG" &
  SIDECAR_PID=$!
  printf '%s\n' "$SIDECAR_PID" > "$SIDECAR_PID_FILE"
  wait "$SIDECAR_PID"
}

main() {
  require_file "$PYTHON_BIN"
  mkdir -p "$LOG_DIR"
  : > "$BACKEND_LOG"
  : > "$SIDECAR_LOG"

  load_env
  require_env "FEISHU_APP_ID"
  require_env "FEISHU_APP_SECRET"
  require_env "FEISHU_APP_TOKEN"
  require_env "FEISHU_DEFAULT_DIALOG_ID"

  start_task_executor
  start_server
  wait_for_backend_ready
  start_sidecar
}

main "$@"
