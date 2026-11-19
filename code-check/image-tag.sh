#!/bin/sh
set -eu
# The tree covers the lock, Dockerfile, contract and worker sources; unrelated monorepo changes reuse the image.
repo=$(printf '%s' "${GITHUB_REPOSITORY:-kaycee7000/ear}" | tr '[:upper:]' '[:lower:]')
tree=$(git rev-parse HEAD:code-check)
printf 'ghcr.io/%s/code-check:tree-%s\n' "$repo" "$tree"
