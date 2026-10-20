#!/usr/bin/env bash
# Local end-to-end demo: mock server + pipeline + protocol upload.
set -euo pipefail
cd "$(dirname "$0")"

PROJECT="${1:?usage: ./run-demo.sh /path/to/project [days]}"
DAYS="${2:-90}"
OUT="${OUT:-./paxel-local-out}"

node mock-server.mjs >/tmp/paxel-local-mock.log 2>&1 &
MOCK_PID=$!
trap 'kill "$MOCK_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 20); do
  curl -sf -m 1 http://127.0.0.1:8899/health >/dev/null && break
  sleep 0.3
done

python3 runner.py "$PROJECT" --days "$DAYS" --out "$OUT"
echo
echo "artifacts: $OUT"
