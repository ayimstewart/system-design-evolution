#!/usr/bin/env bash
# Print which replica answers each request. Try it before and after stage 1,
# and while running `make chaos`.
URL="${1:-http://localhost:8000/whoami}"
while true; do
  out=$(curl -s -m 2 -w ' %{http_code} %{time_total}s' "$URL" || echo "DOWN")
  echo "$(date +%T) $out"
  sleep 0.3
done
