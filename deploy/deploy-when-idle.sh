#!/usr/bin/env bash
# Deploy only when nothing would be interrupted: no evolution round in progress (a deploy changes the pinned
# referee and voids the round) and the worker idle. Run on the server from ~/paperci, e.g. in the background:
#   nohup bash deploy/deploy-when-idle.sh [git-ref] > registry/ops/deploy-when-idle.log 2>&1 &
set -uo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
for _ in $(seq 1 96); do  # give up after 8 hours
  if ! $PY -c "import sys; from paperci import evolve; sys.exit(0 if evolve.has_pending() else 1)" 2>/dev/null &&
     $PY -c "import sys, json; d = json.load(open('registry/ops/heartbeat.json')); sys.exit(0 if not d.get('busy') else 1)" 2>/dev/null; then
    exec bash deploy/deploy.sh "${1:-origin/main}"
  fi
  sleep 300
done
echo "gave up: never idle within 8 hours"
exit 1
