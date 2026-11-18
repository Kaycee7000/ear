#!/bin/sh
set -eu
# The tree includes the lock, Dockerfile, schemas and worker sources; unrelated monorepo changes reuse the image.
repo=$(printf '%s' "$GITHUB_REPOSITORY" | tr '[:upper:]' '[:lower:]')
tree=$(git rev-parse HEAD:data-tools)
printf 'ghcr.io/%s/data-tools:tree-%s\n' "$repo" "$tree"
