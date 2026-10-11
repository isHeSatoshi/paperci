#!/usr/bin/env bash
# One-time (idempotent) setup of the PaperCI cloud server. Run as the `ubuntu` user from ~/paperci:
#   bash deploy/bootstrap.sh <public-hostname> ["YYYY-MM-DD HH:MM:SS" poweroff time in UTC]
# Secrets are NOT handled here: ~/paperci/.env is copied over SSH separately (chmod 600).
set -euo pipefail
HOST="${1:?public hostname, e.g. 3-90-12-34.sslip.io}"
POWEROFF_UTC="${2:-2026-10-16 18:29:00}"
cd "$(dirname "$0")/.."
APP="$PWD"

echo "== packages"
sudo apt-get update -y -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q docker.io python3-venv python3-pip git caddy sqlite3 jq
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"

echo "== swap (builds of ML repos can exceed 8 GB RAM)"
if ! sudo swapon --show | grep -q /swapfile; then
  sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap -q /swapfile && sudo swapon /swapfile
  grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "== reboot on kernel panic/oops/lockup (the VM heals a frozen guest itself; CloudWatch alarms are denied here)"
printf 'kernel.panic = 10\nkernel.panic_on_oops = 1\nkernel.softlockup_panic = 1\nkernel.hung_task_panic = 0\n' \
  | sudo tee /etc/sysctl.d/90-paperci-selfheal.conf >/dev/null
sudo sysctl -q --system

echo "== docker log rotation"
echo '{"log-driver":"json-file","log-opts":{"max-size":"20m","max-file":"3"}}' | sudo tee /etc/docker/daemon.json >/dev/null
sudo systemctl restart docker

echo "== python env"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "== sandbox images"
sudo docker build -q -t paperci/proxy:latest sandbox/proxy
sudo docker build -q -t paperci/sandbox:base sandbox/runner

echo "== services"
test -f .env || { echo "missing $APP/.env (copy it over SSH first)"; exit 1; }
chmod 600 .env
sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo sed -i "s/^OnCalendar=.*/OnCalendar=${POWEROFF_UTC} UTC/" /etc/systemd/system/paperci-poweroff.timer
sed "s/{{HOST}}/${HOST}/" deploy/Caddyfile | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now paperci-web.service paperci-watchdog.timer paperci-poweroff.timer
sudo systemctl restart paperci-web.service
sudo systemctl reload-or-restart caddy

echo "== done: https://${HOST}  (poweroff scheduled ${POWEROFF_UTC} UTC)"
systemctl list-timers --no-pager | grep paperci || true
