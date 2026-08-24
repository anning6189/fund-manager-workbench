#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/fund-manager-workbench}"
BRANCH="${BRANCH:-main}"

cd "$APP_DIR"

git fetch origin "$BRANCH"
git reset --hard "origin/$BRANCH"

sudo systemctl daemon-reload
sudo systemctl restart consumer-research.service
sudo systemctl status consumer-research.service --no-pager -l

curl -fsS "http://127.0.0.1:8765/api/health"
curl -fsS "http://127.0.0.1:8765/api/ops-status" | head -c 1200
echo
