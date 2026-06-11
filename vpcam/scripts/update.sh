#!/usr/bin/env bash
# update.sh — Pull latest VPCam code, sync systemd units, restart services
# Run as pi user from /home/pi/vpcam/
# Usage: bash scripts/update.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== VPCam Update ==="
echo "Repo: ${REPO_DIR}"

# --- Pull latest ---
echo "[1/4] Pulling latest from git..."
cd "${REPO_DIR}"
git pull

# --- Sync systemd units (ExecStart etc. can change between versions) ---
echo "[2/4] Syncing systemd units..."
sudo cp "${REPO_DIR}/systemd/vpcam.service" /etc/systemd/system/vpcam.service
if [ -f "${REPO_DIR}/webui/vpcam_web.service" ]; then
    sudo cp "${REPO_DIR}/webui/vpcam_web.service" /etc/systemd/system/vpcam_web.service
fi
sudo systemctl daemon-reload

# --- Restart services ---
echo "[3/4] Restarting services..."
sudo systemctl restart vpcam
if systemctl list-unit-files vpcam_web.service >/dev/null 2>&1; then
    sudo systemctl restart vpcam_web || true
fi

# --- Status ---
echo "[4/4] Service status:"
sudo systemctl status vpcam --no-pager
