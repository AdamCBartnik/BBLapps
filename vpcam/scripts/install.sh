#!/usr/bin/env bash
# install.sh — Fresh Pi setup for VPCam
# Run as pi user from /home/pi/vpcam/
# Usage: bash scripts/install.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="/etc/vpcam"
IOC_SERVICE_SRC="${REPO_DIR}/systemd/vpcam.service"
IOC_SERVICE_DST="/etc/systemd/system/vpcam.service"
WEB_SERVICE_SRC="${REPO_DIR}/webui/vpcam_web.service"
WEB_SERVICE_DST="/etc/systemd/system/vpcam_web.service"
SUDOERS_DST="/etc/sudoers.d/vpcam-web"

echo "=== VPCam Install ==="
echo "Repo: ${REPO_DIR}"

# --- System packages ---
echo "[1/6] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y python3-pip python3-picamera2 python3-numpy python3-yaml libcamera-apps

# --- Camera selection ---
echo ""
echo "Select camera sensor:"
echo "  1) IMX708  — Raspberry Pi Camera Module 3 (rolling shutter, 4608x2592)"
echo "  2) IMX296  — Raspberry Pi Global Shutter Camera (1456x1088)"
echo "  3) OG02B10 — OmniVision OG02B10 (coming soon)"
echo ""
read -rp "Enter choice [1-3]: " CAMERA_CHOICE

case "${CAMERA_CHOICE}" in
    1)
        SELECTED_CAMERA="imx708"
        CAMERA_OVERLAY="imx708"
        CONFIG_EXAMPLE="config_imx708.yaml.example"
        echo "  Selected: IMX708"
        ;;
    2)
        SELECTED_CAMERA="imx296"
        CAMERA_OVERLAY="imx296,cam1"
        CONFIG_EXAMPLE="config_imx296.yaml.example"
        echo "  Selected: IMX296"
        ;;
    3)
        SELECTED_CAMERA="og02b10"
        CAMERA_OVERLAY="og02b10"
        CONFIG_EXAMPLE="config_og02b10.yaml.example"
        echo "  Selected: OG02B10"
        ;;
    *)
        echo "  Invalid choice. Exiting."
        exit 1
        ;;
esac

# Add dtoverlay if not already present
OVERLAY_LINE="dtoverlay=${CAMERA_OVERLAY}"
CONFIG_TXT="/boot/firmware/config.txt"
if grep -q "dtoverlay=${SELECTED_CAMERA}" "${CONFIG_TXT}" 2>/dev/null; then
    echo "  dtoverlay already set in ${CONFIG_TXT}"
else
    echo "${OVERLAY_LINE}" | sudo tee -a "${CONFIG_TXT}" > /dev/null
    echo "  Added '${OVERLAY_LINE}' to ${CONFIG_TXT}"
    NEED_REBOOT=1
fi

# Verify camera is detected (skip if we just added the overlay — reboot needed first)
if [ -z "${NEED_REBOOT:-}" ]; then
    CAMERA_LIST=$( (rpicam-hello --list-cameras 2>&1 || libcamera-hello --list-cameras 2>&1) || true )
    if echo "${CAMERA_LIST}" | grep -qi "${SELECTED_CAMERA}"; then
        echo "  ✓ ${SELECTED_CAMERA} camera detected."
    else
        echo "  ⚠ WARNING: ${SELECTED_CAMERA} not detected. Check ribbon cable is in CAM1 on the Nano B."
    fi
fi
echo ""

# --- Python packages ---
echo "[2/6] Installing Python packages..."
# rawpy is only used by the imx296_mono driver (DNG decode), but install it
# unconditionally so switching camera type never needs a reinstall
pip3 install --break-system-packages caproto flask pillow rawpy

# pip installs scripts to ~/.local/bin which is not on PATH by default
if ! grep -q 'LOCAL_BIN' ~/.bashrc 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$PATH"  # LOCAL_BIN — added by vpcam install' >> ~/.bashrc
    echo "  Added ~/.local/bin to PATH in ~/.bashrc"
fi
export PATH="$HOME/.local/bin:$PATH"

# --- Config directory ---
echo "[3/6] Setting up config directory..."
sudo mkdir -p "${CONFIG_DIR}"
if [ ! -f "${CONFIG_DIR}/config.yaml" ]; then
    sudo cp "${REPO_DIR}/ioc/${CONFIG_EXAMPLE}" "${CONFIG_DIR}/config.yaml"
    echo "  Copied ${CONFIG_EXAMPLE} → ${CONFIG_DIR}/config.yaml"
    echo "  *** Edit ${CONFIG_DIR}/config.yaml before starting the service! ***"
else
    echo "  Config already exists at ${CONFIG_DIR}/config.yaml — not overwritten."
fi

# --- IOC systemd service ---
echo "[4/6] Installing IOC systemd service..."
sudo cp "${IOC_SERVICE_SRC}" "${IOC_SERVICE_DST}"
sudo systemctl daemon-reload
sudo systemctl enable vpcam

# --- Web UI systemd service ---
echo "[5/6] Installing Web UI systemd service..."
sudo cp "${WEB_SERVICE_SRC}" "${WEB_SERVICE_DST}"
sudo systemctl daemon-reload
sudo systemctl enable vpcam_web

# Allow the web UI to restart the IOC via sudo without a password prompt
if [ ! -f "${SUDOERS_DST}" ]; then
    echo "pi ALL=(ALL) NOPASSWD: /bin/systemctl restart vpcam" | sudo tee "${SUDOERS_DST}" > /dev/null
    sudo chmod 440 "${SUDOERS_DST}"
    echo "  Created sudoers entry for web UI → IOC restart"
fi

# --- MOTD login banner ---
echo "[6/6] Installing MOTD login banner..."
MOTD_SRC="${REPO_DIR}/scripts/vpcam-motd.sh"
MOTD_DST="/etc/update-motd.d/50-vpcam"
sudo cp "${MOTD_SRC}" "${MOTD_DST}"
sudo chmod +x "${MOTD_DST}"
# Disable the default "last login" line if not already done
sudo sed -i 's/^PrintLastLog yes/PrintLastLog no/' /etc/ssh/sshd_config 2>/dev/null || true
echo "  MOTD installed → shown on every SSH login"

# --- Done ---
echo "Done."
echo ""
if [ -n "${NEED_REBOOT:-}" ]; then
    echo "*** A reboot is required to activate the camera overlay. ***"
    echo ""
    read -rp "Reboot now? [y/N]: " DO_REBOOT
    if [[ "${DO_REBOOT}" =~ ^[Yy]$ ]]; then
        echo "Rebooting..."
        sudo reboot
    else
        echo "Remember to reboot before starting the IOC."
    fi
else
    echo "Next steps:"
    echo "  1. Edit /etc/vpcam/config.yaml (set hostname, prefix, ROI, calibration)"
    echo "  2. sudo systemctl start vpcam      # start IOC"
    echo "  3. sudo systemctl start vpcam_web  # start web UI"
    echo "  4. caproto-get <prefix>:cam1:Model_RBV   # verify IOC is up (prefix set in config.yaml)"
    echo "  5. Open http://\$(hostname -I | awk '{print \$1}'):8080  # web dashboard"
    echo ""
    echo "The login banner will show the web UI URL on every SSH login."
fi
