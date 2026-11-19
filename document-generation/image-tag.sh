#!/bin/sh
set -eu
# Tagged by the module's Git tree: a change to the renderers or lock builds a new image; other changes reuse it.
repo=$(printf '%s' "${GITHUB_REPOSITORY:-kaycee7000/ear}" | tr '[:upper:]' '[:lower:]')
tree=$(git rev-parse HEAD:document-generation)
printf 'ghcr.io/%s/document-generation:tree-%s\n' "$repo" "$tree"
