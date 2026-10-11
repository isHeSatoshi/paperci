#!/bin/sh
# Last-resort healer: runs only when the Python watchdog itself crashed or hung (bad deploy, broken venv, bug).
# Plain POSIX shell, no Python, no repo imports, so whatever broke the watchdog cannot break this.
APP=/home/ubuntu/paperci
EV="$APP/registry/ops/events.jsonl"
now() { date +%s; }
log() {  # kind, text, healed
  printf '{"ts": %s, "kind": "%s", "text": "%s", "healed": %s, "component": "fallback"}\n' "$(now)" "$1" "$2" "$3" >> "$EV"
  chown ubuntu:ubuntu "$EV" 2>/dev/null
}
log error "Python watchdog failed; the shell fallback healer ran instead" false
if ! timeout 20 docker info >/dev/null 2>&1; then
  systemctl restart docker
  if timeout 20 docker info >/dev/null 2>&1; then log incident "Fallback healer restarted the Docker daemon" true
  else log incident "Fallback healer could not restart Docker" false; fi
fi
if ! curl -sf -m 10 http://127.0.0.1:8000/healthz >/dev/null; then
  systemctl restart paperci-web
  sleep 15
  if curl -sf -m 10 http://127.0.0.1:8000/healthz >/dev/null; then log incident "Fallback healer restarted the web app" true
  else log incident "Fallback healer restarted the web app but it is still unhealthy" false; fi
fi
exit 0
