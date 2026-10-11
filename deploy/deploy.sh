#!/usr/bin/env bash
# Safe deploy with automatic rollback. Run on the server as ubuntu from ~/paperci:
#   bash deploy/deploy.sh [git-ref]          (default: origin/main)
# 1. fetch + switch to the new commit   2. install deps, import check, offline tests
# 3. restart and wait for /healthz      4. any failure -> back to the previous commit, restarted
# The last commit that passed is recorded so the watchdog can roll back to it on its own later.
set -uo pipefail
cd "$(dirname "$0")/.."
REF="${1:-origin/main}"
PY=.venv/bin/python
GOOD=$(git rev-parse HEAD)
ev() { $PY -m paperci.ops event deploy "$1" "passed=$2" >/dev/null 2>&1 || true; }
healthy() { for _ in $(seq 1 30); do curl -sf -m 5 http://127.0.0.1:8000/healthz >/dev/null && return 0; sleep 3; done; return 1; }
rollback() {
  echo "!! $1 -> rolling back to ${GOOD:0:8}"
  git reset -q --hard "$GOOD"
  sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/ && sudo systemctl daemon-reload
  sudo systemctl restart paperci-web
  if healthy; then ev "Deploy of ${NEW:0:8} rejected ($1); rolled back to ${GOOD:0:8}, service healthy" true
  else ev "Deploy of ${NEW:0:8} rejected ($1); rolled back to ${GOOD:0:8} but service is unhealthy" false; fi
  exit 1
}
git fetch -q origin || { echo "fetch failed"; exit 1; }
NEW=$(git rev-parse "$REF") || exit 1
[ "$NEW" = "$GOOD" ] && [ -z "${PAPERCI_DEPLOY_INJECT_FAULT:-}" ] && { echo "already at ${NEW:0:8}"; echo "$GOOD" > registry/ops/last_good_commit; exit 0; }
git reset -q --hard "$NEW"
if [ -n "${PAPERCI_DEPLOY_INJECT_FAULT:-}" ]; then  # healer drill: simulate a broken commit
  echo "def broken(:" >> paperci/board.py
fi
$PY -m pip install -q -r requirements.txt || rollback "dependency install failed"
$PY -c "import paperci.web, paperci.ops, paperci.board, paperci.pipeline" || rollback "import check failed"
$PY tests/test_offline.py > /tmp/paperci-tests.log 2>&1 || rollback "offline tests failed"
sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl restart paperci-web
healthy || rollback "service unhealthy after restart"
echo "$NEW" > registry/ops/last_good_commit
ev "Deployed ${NEW:0:8} (tests passed, service healthy)" true
echo "deployed ${NEW:0:8}"
