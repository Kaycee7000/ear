#!/usr/bin/env bash
# Entry point of the modeling job: the GPU worker when the runner has a working NVIDIA GPU,
# otherwise the CPU worker (GitHub's free ubuntu-latest runners).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
  exec bash "$here/gpu-worker.sh"
fi
echo "No NVIDIA GPU on this runner: using the CPU worker."
exec bash "$here/cpu-worker.sh"
