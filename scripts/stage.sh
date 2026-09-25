#!/usr/bin/env bash
# Run docker compose with every overlay up to and including a stage.
#   scripts/stage.sh 3 up -d     -> 00 + 01 + 02 + 03
set -euo pipefail
cd "$(dirname "$0")/.."
stage="${1:?usage: scripts/stage.sh <0-9> [docker compose args...]}"
shift
[[ "$stage" =~ ^[0-9]$ ]] || { echo "stage must be 0-9" >&2; exit 1; }
files=()
for f in compose/0*.yml; do
  n=$(basename "$f" | cut -c1-2)
  (( 10#$n <= stage )) && files+=(-f "$f")
done
exec ${COMPOSE:-docker compose} "${files[@]}" "$@"
