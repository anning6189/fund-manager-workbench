#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/fund-manager-workbench}"
SERVICE_USER="${SERVICE_USER:-root}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
SYNC_TIMEZONE="${SYNC_TIMEZONE:-Asia/Shanghai}"

if [ ! -d "$APP_DIR" ]; then
  echo "APP_DIR does not exist: $APP_DIR" >&2
  exit 1
fi

if [ ! -x "$PYTHON_BIN" ]; then
  echo "Python runtime not found or not executable: $PYTHON_BIN" >&2
  exit 1
fi

cd "$APP_DIR"

sudo timedatectl set-timezone "$SYNC_TIMEZONE" || true

sudo install -m 0644 deploy/consumer-research-daily-sync.service /etc/systemd/system/consumer-research-daily-sync.service
sudo install -m 0644 deploy/consumer-research-daily-sync.timer /etc/systemd/system/consumer-research-daily-sync.timer

sudo systemctl daemon-reload
sudo systemctl enable --now consumer-research-daily-sync.timer

echo "Installed daily sync timer."
echo
systemctl list-timers consumer-research-daily-sync.timer --no-pager
echo
echo "To run once now:"
echo "  sudo systemctl start consumer-research-daily-sync.service"
echo
echo "To view logs:"
echo "  sudo journalctl -u consumer-research-daily-sync.service -n 120 --no-pager"
echo "  tail -n 120 $APP_DIR/data/monitoring/module3-realtime-research/server-daily-sync.log"
