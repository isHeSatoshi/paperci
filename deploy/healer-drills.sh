#!/usr/bin/env bash
# Drills against the self-healing layer itself. Run on the server from ~/paperci:  bash deploy/healer-drills.sh
# Refuses to run while the worker is busy with a paper (the drills restart services).
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
ev() { $PY -m paperci.ops event drill "$1" "passed=$2" >/dev/null 2>&1; echo "[$2] $1"; }
ok() { curl -sf -m 5 http://127.0.0.1:8000/healthz >/dev/null; }
wait_ok() { local t0=$(date +%s); for _ in $(seq 1 ${1:-40}); do ok && { echo $(( $(date +%s) - t0 )); return 0; }; sleep 3; done; return 1; }
mainpid() { systemctl show -p MainPID --value paperci-web; }
watchdog_once() { sudo systemctl start paperci-watchdog.service; }

if $PY -c "import sys; from paperci import ops; sys.exit(0 if ops.busy() else 1)"; then
  echo "worker is busy with a paper; try again later"; exit 2
fi
ok || { echo "service not healthy before drills; aborting"; exit 2; }

# 1. The agent process dies (crash, OOM kill): systemd must restart it.
kill -9 "$(mainpid)"; sleep 1
if s=$(wait_ok 40); then ev "Agent process killed with SIGKILL: systemd restarted it, healthy again in ${s}s" true
else ev "Agent process killed with SIGKILL: NOT healthy after 120s" false; fi

# 2. The agent hangs (process alive, nothing answers): the watchdog must notice and restart it.
sleep 5; P=$(mainpid); kill -STOP "$P"
watchdog_once; watchdog_once
if s=$(wait_ok 40); then ev "Agent frozen with SIGSTOP (alive but unresponsive): watchdog detected it on its 2nd check and restarted it; healthy ${s}s later" true
else ev "Agent frozen with SIGSTOP: watchdog did NOT recover it" false; kill -CONT "$P" 2>/dev/null; fi

# 3. The watchdog itself is broken (bad code, broken venv): the shell fallback healer must take over.
sudo systemctl stop paperci-web
sudo PAPERCI_WATCHDOG_SIMULATE_CRASH=1 /bin/sh -c "timeout 600 $PWD/.venv/bin/python -m paperci.ops watchdog >/dev/null 2>&1 || /bin/sh $PWD/deploy/fallback-healer.sh"
if s=$(wait_ok 20); then ev "Python watchdog crashed while the agent was down: the shell fallback healer restarted the agent" true
else ev "Python watchdog crashed while the agent was down: fallback did NOT recover it" false; sudo systemctl start paperci-web; fi

# 4. The Docker daemon goes down (sandboxes, egress proxy gone): the watchdog must bring both back.
sudo systemctl stop docker.socket docker
watchdog_once
if timeout 20 docker info >/dev/null 2>&1 && [ "$(docker inspect -f '{{.State.Running}}' paperci-egress 2>/dev/null)" = "true" ]; then
  ev "Docker daemon stopped: watchdog restarted Docker and the sandbox egress proxy is running again" true
else ev "Docker daemon stopped: NOT fully recovered" false; sudo systemctl start docker; fi
wait_ok 20 >/dev/null

# 5. A broken commit is deployed: checks must reject it and roll back with the service still up.
BEFORE=$(git rev-parse HEAD)
PAPERCI_DEPLOY_INJECT_FAULT=1 bash deploy/deploy.sh HEAD >/tmp/drill-deploy.log 2>&1
rc=$?
if [ $rc -ne 0 ] && [ "$(git rev-parse HEAD)" = "$BEFORE" ] && git diff --quiet && wait_ok 20 >/dev/null; then
  ev "Broken commit deployed (syntax error injected): import check rejected it, rolled back, service healthy" true
else ev "Broken commit deployed: rollback did NOT complete cleanly (rc=$rc)" false; git checkout -q -- . ; fi
