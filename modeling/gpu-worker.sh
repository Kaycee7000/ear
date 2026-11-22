#!/usr/bin/env bash
set -euo pipefail

[[ "${EAR_REQUEST_ID:-}" =~ ^[0-9a-f]{32}$ ]] || { echo "Invalid request ID." >&2; exit 2; }
nvidia-smi --query-gpu=name --format=csv,noheader
container_id=""
cleanup() { if [[ -n "$container_id" ]]; then docker rm -f "$container_id" >/dev/null || true; fi; }
trap cleanup EXIT

# Same cache as cpu-worker.sh: a self-hosted GPU runner keeps the checksum-pinned GGUF between runs instead of
# downloading 2 GB for every design (GitHub's runners start empty, so nothing changes there).
model_dir=${EAR_MODEL_CACHE:-$HOME/.cache/ear-model}
model=$model_dir/qwen2.5-3b-instruct-q4_k_m.gguf
model_url=https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/7dabda4d13d513e3e842b20f0d435c732f172cbe/qwen2.5-3b-instruct-q4_k_m.gguf
model_sha=626b4a6678b86442240e33df819e00132d3ba7dddfe1cdc4fbb18e0a9615c62d
if ! echo "$model_sha  $model" | sha256sum --check --status 2>/dev/null; then
  mkdir -p "$model_dir"
  curl --fail --silent --show-error --location --retry 2 --max-time 600 "$model_url" -o "$model.part"
  echo "$model_sha  $model.part" | sha256sum --check --status
  mv "$model.part" "$model"
fi

# Only the worker process receives storage credentials. The public job logs contain no prompts.
image=ghcr.io/ggml-org/llama.cpp@sha256:ae96e52f8b2106ea82bad786ee2d08bb9b0a8c93403df9c27ae1bf065a118089
container_id=$(docker run -d --gpus all --log-driver none -p 127.0.0.1::8080 \
  -v "$model:/models/design.gguf:ro" "$image" \
  -m /models/design.gguf --alias ear-model --host 0.0.0.0 --port 8080 \
  --ctx-size 16384 --parallel 1 --device CUDA0 --n-gpu-layers 99)
address=$(docker port "$container_id" 8080/tcp)
export EAR_MODEL_URL="http://$address"
for ((i=0; i<90; i++)); do
  if curl --fail --silent --max-time 2 "$EAR_MODEL_URL/health" >/dev/null; then
    python jobs.py
    exit 0
  fi
  [[ "$(docker inspect -f '{{.State.Running}}' "$container_id")" == true ]] || break
  sleep 2
done
echo "CUDA model server failed to become ready." >&2
exit 1
