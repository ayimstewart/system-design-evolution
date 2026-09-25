#!/usr/bin/env bash
# Crash one container of a service while traffic is flowing.
#   scripts/chaos.sh app          scripts/chaos.sh db-replica
# From stage 8 on, restart policies bring it back and the LB routes around it.
set -euo pipefail
target="${1:-app}"
victim=$(docker ps --filter "label=com.docker.compose.project=sde" \
                   --filter "label=com.docker.compose.service=$target" \
                   --format '{{.Names}}' | head -n1)
[ -n "$victim" ] || { echo "no running '$target' container" >&2; exit 1; }
echo "crashing $victim"
# Kill PID 1 from inside (a "crash"), rather than `docker kill`, which Docker
# treats as a deliberate stop and won't auto-restart.
docker exec "$victim" sh -c 'kill -TERM 1' || true
