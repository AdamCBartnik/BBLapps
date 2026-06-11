#!/bin/bash
# /etc/update-motd.d/50-vpcam
#
# Dynamic login banner for VPCam devices.
# Installed by scripts/install.sh to /etc/update-motd.d/50-vpcam
#
# Shows hostname, IP, web UI URL, IOC service status, uptime, and CPU temp
# whenever someone logs in via SSH or the local console.

BLUE='\033[0;34m'
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
RESET='\033[0m'

# ── Gather info ───────────────────────────────────────────────────────────────

HOSTNAME=$(hostname)

# Primary outbound IP (same UDP-socket trick as the IOC uses)
IP=$(python3 -c "
import socket
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(('8.8.8.8', 80))
    print(s.getsockname()[0])
    s.close()
except Exception:
    print('unknown')
" 2>/dev/null)

# Web UI port (read from config if available, else default 8080)
WEBUI_PORT=8080
if [ -f /etc/vpcam/config.yaml ]; then
    PORT_FROM_CFG=$(grep -Po '(?<=port:\s)\d+' /etc/vpcam/config.yaml 2>/dev/null | head -1)
    [ -n "$PORT_FROM_CFG" ] && WEBUI_PORT=$PORT_FROM_CFG
fi

WEBUI_URL="http://${IP}:${WEBUI_PORT}"

# Uptime
UPTIME=$(uptime -p 2>/dev/null || echo "unknown")

# CPU temperature (vcgencmd preferred; fall back to thermal zone sysfs)
if command -v vcgencmd &>/dev/null; then
    CPU_TEMP=$(vcgencmd measure_temp 2>/dev/null | grep -Po '[\d.]+')
else
    TEMP_RAW=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null)
    CPU_TEMP=$(awk "BEGIN {printf \"%.1f\", ${TEMP_RAW:-0}/1000}")
fi

# IOC service status
IOC_STATUS=$(systemctl is-active vpcam 2>/dev/null || echo "unknown")
case "$IOC_STATUS" in
    active)      IOC_COLOR=$GREEN  ;;
    activating)  IOC_COLOR=$YELLOW ;;
    *)           IOC_COLOR=$RED    ;;
esac

# Web service status
WEB_STATUS=$(systemctl is-active vpcam-web 2>/dev/null || echo "unknown")
case "$WEB_STATUS" in
    active)      WEB_COLOR=$GREEN  ;;
    activating)  WEB_COLOR=$YELLOW ;;
    *)           WEB_COLOR=$RED    ;;
esac

# ── Banner ────────────────────────────────────────────────────────────────────

echo ""
echo -e "${BLUE}${BOLD}  ╔══════════════════════════════════════════════╗${RESET}"
echo -e "${BLUE}${BOLD}  ║            PortView 275 / VPCam              ║${RESET}"
echo -e "${BLUE}${BOLD}  ║      Scintillator Viewport Camera System     ║${RESET}"
echo -e "${BLUE}${BOLD}  ╚══════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  ${BOLD}Hostname  :${RESET}  ${CYAN}${HOSTNAME}${RESET}"
echo -e "  ${BOLD}IP Address:${RESET}  ${CYAN}${IP}${RESET}"
echo -e "  ${BOLD}Web UI    :${RESET}  ${CYAN}${WEBUI_URL}${RESET}"
echo ""
echo -e "  ${BOLD}IOC Service :${RESET}  ${IOC_COLOR}${IOC_STATUS}${RESET}"
echo -e "  ${BOLD}Web Service :${RESET}  ${WEB_COLOR}${WEB_STATUS}${RESET}"
echo ""
echo -e "  ${BOLD}Uptime    :${RESET}  ${UPTIME}"
echo -e "  ${BOLD}CPU Temp  :${RESET}  ${CPU_TEMP} °C"
echo ""
echo -e "  ${YELLOW}Useful commands:${RESET}"
echo -e "    sudo systemctl status vpcam       # IOC logs"
echo -e "    sudo systemctl status vpcam-web   # web UI logs"
echo -e "    sudo journalctl -u vpcam -f       # follow IOC log"
echo -e "    watch -n2 vcgencmd measure_temp   # monitor temperature"
echo ""
