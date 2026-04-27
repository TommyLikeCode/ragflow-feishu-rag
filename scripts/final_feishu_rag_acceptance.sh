#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="${BASE_URL:-http://127.0.0.1:9380}"
PYTHON_BIN="${PYTHON_BIN:-python}"
MIN_SCORE="${MIN_SCORE:-0.8}"

CURL_OPTS=(--silent --show-error --fail --max-time 10)

log() {
  printf '[acceptance] %s\n' "$*"
}

fail() {
  printf '[acceptance][FAIL] %s\n' "$*" >&2
  exit 1
}

pass() {
  printf '[acceptance][PASS] %s\n' "$*"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Missing command: $1"
}

check_http_html() {
  local name="$1"
  local url="$2"
  local body
  body="$(curl "${CURL_OPTS[@]}" "$url")" || fail "$name request failed: $url"
  [[ -n "$body" ]] || fail "$name returned empty body"
  pass "$name"
}

check_http_json() {
  local name="$1"
  local url="$2"
  local tmp
  tmp="$(mktemp)"

  curl "${CURL_OPTS[@]}" "$url" >"$tmp" || fail "$name request failed: $url"
  [[ -s "$tmp" ]] || fail "$name returned empty body"

  "$PYTHON_BIN" - "$name" "$tmp" <<'PY'
import json
import sys

name = sys.argv[1]
path = sys.argv[2]
try:
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
except Exception as exc:
    raise SystemExit(f"{name} returned non-JSON payload: {exc}")
if not isinstance(payload, dict):
    raise SystemExit(f"{name} returned non-object JSON")
code = payload.get("code")
if code != 0:
    raise SystemExit(f"{name} returned non-zero code: {code}")
print(f"{name} code={code}")
PY

  rm -f "$tmp"
  pass "$name"
}

get_default_dialog_id() {
  local tmp
  tmp="$(mktemp)"

  curl "${CURL_OPTS[@]}" "$BASE_URL/api/admin/defaults" >"$tmp" || fail "Failed to fetch /api/admin/defaults"
  [[ -s "$tmp" ]] || fail "/api/admin/defaults returned empty body"

  local dialog_id
  dialog_id="$($PYTHON_BIN - "$tmp" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    payload = json.load(f)
if not isinstance(payload, dict):
    raise SystemExit("/api/admin/defaults returned non-object JSON")
if payload.get("code") != 0:
    raise SystemExit(f"/api/admin/defaults returned non-zero code: {payload.get('code')}")
data = payload.get("data") or {}
if not isinstance(data, dict):
    raise SystemExit("/api/admin/defaults payload missing data object")
dialog_id = str(data.get("default_dialog_id") or "").strip()
if not dialog_id:
    raise SystemExit("/api/admin/defaults returned empty default_dialog_id")
print(dialog_id)
PY
)"

  rm -f "$tmp"
  printf '%s' "$dialog_id"
}

run_eval_and_assert_thresholds() {
  EVAL_GENERATION_LEVEL="PASS"
  local dialog_id
  dialog_id="${FEISHU_DEFAULT_DIALOG_ID:-}"
  if [[ -z "$dialog_id" ]]; then
    dialog_id="$(get_default_dialog_id)"
  fi

  local out_file
  out_file="$(mktemp)"
  log "Running eval script with dialog_id=$dialog_id"
  (
    cd "$ROOT_DIR"
    "$PYTHON_BIN" scripts/eval_feishu_rag.py --dialog-id "$dialog_id" --output "$out_file" >/dev/null
  ) || {
    rm -f "$out_file"
    fail "Eval script failed to produce JSON"
  }

  [[ -s "$out_file" ]] || {
    rm -f "$out_file"
    fail "Eval script did not produce JSON"
  }

  "$PYTHON_BIN" - "$out_file" "$MIN_SCORE" <<'PY'
import json
import sys

path = sys.argv[1]
threshold = float(sys.argv[2])
with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)

required = [
    "hit_rate",
    "answer_keyword_match_rate",
    "source_keyword_match_rate",
    "citation_coverage_rate",
    "avg_citation_count",
]

warnings = []
source_pass = False
for key in required:
    value = float(data.get(key, 0.0))
    print(f"{key}={value:.4f}")
    if key == "source_keyword_match_rate":
        source_pass = value >= threshold
        if not source_pass:
            raise SystemExit(f"source_keyword_match_rate below threshold {threshold:.2f}: {value:.4f}")
    elif key in {"hit_rate", "answer_keyword_match_rate", "citation_coverage_rate"} and value < threshold:
        warnings.append(f"{key}={value:.4f}")

if warnings:
    print("WARN: " + ", ".join(warnings))

print("Eval retrieval acceptance: PASS" if source_pass else "Eval retrieval acceptance: FAIL")
PY

  EVAL_GENERATION_LEVEL="$($PYTHON_BIN - "$out_file" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
  data = json.load(f)
for key in ["hit_rate", "answer_keyword_match_rate", "citation_coverage_rate"]:
  if float(data.get(key, 0.0)) < 0.8:
    print("WARN")
    break
else:
  print("PASS")
PY
)"

  rm -f "$out_file"
}

main() {
  require_cmd curl
  require_cmd "$PYTHON_BIN"

  log "BASE_URL=$BASE_URL"

  local_dialog_id="${FEISHU_DEFAULT_DIALOG_ID:-}"
  if [[ -z "$local_dialog_id" ]]; then
    local_dialog_id="$(get_default_dialog_id)"
  fi
  FEISHU_DEFAULT_DIALOG_ID="$local_dialog_id"

  check_http_html "/admin" "$BASE_URL/admin"
  check_http_json "/api/v1/feishu/ping" "$BASE_URL/api/v1/feishu/ping"
  check_http_json "/api/v1/feishu/metrics" "$BASE_URL/api/v1/feishu/metrics"
  check_http_json "/api/v1/feishu/health" "$BASE_URL/api/v1/feishu/health"

  check_http_json "/api/v1/feishu/debug_acl" "$BASE_URL/api/v1/feishu/debug_acl?dialog_id=${FEISHU_DEFAULT_DIALOG_ID}&open_id=${FEISHU_DEBUG_OPEN_ID:-ou_test_citation}"

  check_http_json "/api/admin/kbs" "$BASE_URL/api/admin/kbs?page=1&page_size=10"
  check_http_json "/api/admin/documents" "$BASE_URL/api/admin/documents?page=1&page_size=10"
  check_http_json "/api/admin/kb-selections" "$BASE_URL/api/admin/kb-selections"

  if ! run_eval_and_assert_thresholds; then
    fail "Eval retrieval acceptance failed"
  fi

  log "API acceptance: PASS"
  log "Eval retrieval acceptance: PASS"
  log "Eval generation/citation metrics: ${EVAL_GENERATION_LEVEL:-PASS}"
  if [[ "${EVAL_GENERATION_LEVEL:-PASS}" == "WARN" ]]; then
    log "Final acceptance: PASS with warnings"
  else
    log "Final acceptance: PASS"
  fi
}

main "$@"
