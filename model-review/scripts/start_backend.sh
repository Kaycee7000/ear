#!/usr/bin/env bash
# Start (or find) an OpenAI-compatible server for the model under review.
#
# Env in:  BACKEND (ollama | endpoint | mock), MODEL, ENDPOINT (endpoint only),
#          MODEL_REVIEW_API_KEY (optional, endpoint only)
# Env out: MODEL_REVIEW_BASE_URL, appended to $GITHUB_ENV when running in Actions.
#
# Works on GitHub-hosted Linux runners and on self-hosted runners (Linux, macOS,
# or Windows with Git Bash). If Ollama is already running on a self-hosted
# machine, it is reused rather than started again.
set -euo pipefail

BACKEND="${BACKEND:?BACKEND is required}"
MODEL="${MODEL:?MODEL is required}"
LOG_DIR="${RUNNER_TEMP:-/tmp}/model-review"
mkdir -p "$LOG_DIR"

emit_base_url() {
  if [[ -n "${GITHUB_ENV:-}" ]]; then
    echo "MODEL_REVIEW_BASE_URL=$1" >> "$GITHUB_ENV"
  fi
  echo "MODEL_REVIEW_BASE_URL=$1"
}

# wait_for URL SECONDS [extra curl args...]
wait_for() {
  local url=$1 seconds=$2
  shift 2
  local deadline=$((SECONDS + seconds))
  while (( SECONDS < deadline )); do
    if curl -fsS --max-time 5 "$@" "$url" > /dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

# fail MESSAGE: report the error, leave a results.json saying why (so the agent
# gets a reason, not just "step failed"), and stop.
fail() {
  echo "::error::$1"
  local py
  py=$(command -v python || command -v python3 || true)
  if [[ -n "$py" ]]; then
    "$py" - "$1" "$MODEL" "$BACKEND" "$LOG_DIR/results.json" <<'PY' || true
import json, sys
error, model, backend, path = sys.argv[1:5]
with open(path, "w", encoding="utf-8") as fh:
    json.dump({"schema": "model-review/v1", "verdict": "ERROR", "error": error, "stage": "backend",
               "model": model, "backend": backend, "suite": None,
               "summary": {}, "gates": [], "cases": []}, fh, indent=2)
PY
  fi
  exit 1
}

gpu_info() {
  if command -v nvidia-smi > /dev/null 2>&1; then
    echo "::group::GPU"
    nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader || true
    echo "::endgroup::"
  else
    echo "No NVIDIA GPU visible (nvidia-smi not found); the model will run on CPU."
  fi
}

case "$BACKEND" in
  ollama)
    OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
    gpu_info
    if ! curl -fsS --max-time 5 "$OLLAMA_URL/api/tags" > /dev/null 2>&1; then
      if ! command -v ollama > /dev/null 2>&1; then
        if [[ "$(uname -s)" != "Linux" ]]; then
          fail "Ollama is not installed on this runner. Install it, or use backend=endpoint."
        fi
        echo "::group::Install Ollama"
        curl -fsSL https://ollama.com/install.sh | sh
        echo "::endgroup::"
      fi
      # The installer may already have started a service.
      if ! curl -fsS --max-time 5 "$OLLAMA_URL/api/tags" > /dev/null 2>&1; then
        nohup ollama serve > "$LOG_DIR/backend.log" 2>&1 &
        if ! wait_for "$OLLAMA_URL/api/tags" 90; then
          cat "$LOG_DIR/backend.log" || true
          fail "Ollama did not start (see backend.log in the artifact)"
        fi
      fi
    else
      echo "Reusing the Ollama server already running at $OLLAMA_URL"
    fi
    echo "::group::Pull $MODEL"
    if ! ollama pull "$MODEL"; then
      echo "::endgroup::"
      fail "ollama pull '$MODEL' failed. Check the model name/tag (e.g. qwen2.5:0.5b or hf.co/<user>/<repo>:<quant>)."
    fi
    echo "::endgroup::"
    emit_base_url "$OLLAMA_URL/v1"
    ;;

  endpoint)
    if [[ -z "${ENDPOINT:-}" ]]; then
      fail "backend=endpoint needs the endpoint input (an OpenAI-compatible base URL)"
    fi
    BASE="${ENDPOINT%/}"
    auth=()
    if [[ -n "${MODEL_REVIEW_API_KEY:-}" ]]; then
      auth=(-H "Authorization: Bearer $MODEL_REVIEW_API_KEY")
    fi
    # ${auth[@]+...} keeps bash 3.2 (macOS) happy under `set -u` when auth is empty.
    if ! wait_for "$BASE/models" 60 ${auth[@]+"${auth[@]}"}; then
      fail "$BASE/models did not answer. Is the server running and reachable from this runner?"
    fi
    emit_base_url "$BASE"
    ;;

  mock)
    PORT="${MOCK_PORT:-18080}"
    nohup python -m model_review.mock_server --port "$PORT" > "$LOG_DIR/backend.log" 2>&1 &
    if ! wait_for "http://127.0.0.1:$PORT/v1/models" 30; then
      cat "$LOG_DIR/backend.log" || true
      fail "Mock server did not start"
    fi
    emit_base_url "http://127.0.0.1:$PORT/v1"
    ;;

  *)
    fail "Unknown backend '$BACKEND' (expected ollama, endpoint or mock)"
    ;;
esac
