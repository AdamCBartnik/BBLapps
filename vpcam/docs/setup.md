# VPCam Setup Guide

## Prerequisites

- Raspberry Pi CM5 on Waveshare Nano B baseboard
- Raspberry Pi OS Lite 64-bit (Bookworm) flashed to eMMC
- Supported camera connected via CSI ribbon (see supported cameras below)
- LED controller connected via GPIO (BCM 18, physical pin 12)
- Ethernet connected to lab network

**Supported cameras:**

| Camera | Sensor | Resolution | Shutter |
|---|---|---|---|
| Raspberry Pi Camera Module 3 | IMX708 | 4608×2592 | Rolling |
| Raspberry Pi Global Shutter Camera | IMX296 | 1456×1088 | Global |

> **Hardware:** On the Waveshare CM5 Nano B, always use **Camera Slot 1** (labeled `CAM1`). Camera Slot 0 (`CAM0`) will not work with the standard driver configuration.

---

## 1. Flash the OS

Flash Raspberry Pi OS Lite 64-bit (Bookworm) to the CM5 eMMC using `rpiboot` and Raspberry Pi Imager.

**Imager advanced settings (gear icon):**
- Hostname: `vpcam-01` (or your instance number)
- Enable SSH (password authentication)
- Username: `pi`
- Password: `vpcam2026`
- Set Wi-Fi only if needed for initial setup

> The default credentials (`pi` / `vpcam2026`) are for lab use only. Change the password before deploying to a customer site: `passwd`

---

## 2. First Boot

SSH into the Pi:

```bash
ssh pi@vpcam-01.local
```

Update the system:

```bash
sudo apt-get update && sudo apt-get upgrade -y
```

---

## 3. Clone the Repo

```bash
git clone https://github.com/xelera/vpcam.git /home/pi/vpcam
```

---

## 4. Run the Install Script

```bash
cd /home/pi/vpcam
bash scripts/install.sh
```

The script will prompt you to select your camera:

```
Select camera sensor:
  1) IMX708  — Raspberry Pi Camera Module 3 (rolling shutter, 4608x2592)
  2) IMX296  — Raspberry Pi Global Shutter Camera (1456x1088)
  3) OG02B10 — OmniVision OG02B10 (coming soon)

Enter choice [1-3]:
```

The script then:
- Adds the correct `dtoverlay` to `/boot/firmware/config.txt` if not already present
- Installs system and Python dependencies
- Adds `~/.local/bin` to `PATH` so caproto tools are available
- Copies the matching config template to `/etc/vpcam/config.yaml`
- Installs and enables the `vpcam.service` and `vpcam_web.service` systemd units
- Offers to reboot if the overlay was just added (reboot required to activate the camera)

After rebooting, verify the camera is detected:

```bash
rpicam-hello --list-cameras
```

Expected output for IMX296:
```
Available cameras
-----------------
0 : imx296 [1456x1088] (/base/axi/...)
    Modes: 'SRGGB10_CSI2P' : 1456x1088 [60.38 fps - (0, 0)/1456x1088 crop]
```

Expected output for IMX708:
```
Available cameras
-----------------
0 : imx708 [4608x2592 10-bit RGGB] (...)
    Modes: 'SRGGB10_CSI2P' : 2304x1296 ...
           'SRGGB10_CSI2P' : 4608x2592 ...
```

**If the camera is not detected:**
- Confirm you are using CAM1, not CAM0, on the Nano B baseboard
- Check the CSI ribbon cable is fully seated and locked at both ends
- Confirm the overlay is present: `grep dtoverlay /boot/firmware/config.txt`
- Check for driver errors: `sudo dmesg | grep -i imx`

Verify the config was copied:

```bash
cat /etc/vpcam/config.yaml
```

If the file is missing for any reason, copy it manually (replace `imx296` with your camera type):

```bash
sudo mkdir -p /etc/vpcam
sudo cp /home/pi/vpcam/ioc/config_imx296.yaml.example /etc/vpcam/config.yaml
sudo chown pi:pi /etc/vpcam /etc/vpcam/config.yaml
```

> **Permissions:** `/etc/vpcam/config.yaml` must be owned by `pi`. The IOC and web service both run as `pi` and need write access. If you see `Permission denied` errors, run the `chown` commands above.

---

## 5. Edit the Config

> ⚠️ Do this before starting the service. The IOC reads config at startup only.

```bash
nano /etc/vpcam/config.yaml
```

Key fields to update:

| Field | Description |
|---|---|
| `device.hostname` | Must match the Pi hostname |
| `device.instance_id` | Used in PV prefix, e.g. `"01"` |
| `epics.prefix` | Full PV prefix, e.g. `VPCAM:01` |
| `epics.beacon_address` | CA broadcast address for your network |
| `camera.type` | Set by install script — `imx708`, `imx296`, or `og02b10` |
| `roi.*` | Initial ROI — leave at full sensor resolution to start |
| `calibration.*` | mm/pixel values after optical calibration |

---

## 6. Start the Services

```bash
sudo systemctl start vpcam
sudo systemctl status vpcam

sudo systemctl start vpcam_web
sudo systemctl status vpcam_web
```

The web dashboard will be available at **http://vpcam-01.local:8080** from any browser on the same network.

---

## 7. Verify EPICS Connection

**From the Pi itself** (quickest check):

```bash
caproto-get VPCAM:01:cam1:Hostname_RBV
caproto-get VPCAM:01:cam1:IpAddr_RBV
caproto-get VPCAM:01:cam1:CpuTemp_RBV
```

**From a client machine** with caproto installed:

```bash
caproto-monitor VPCAM:01:cam1:Hostname_RBV
caproto-monitor VPCAM:01:cam1:IpAddr_RBV
```

> ⚠️ **Image PVs require a large CA array size.** Frame sizes vary by camera — up to ~34 MB for IMX708 full-res color. Set this on any client machine before connecting:
> ```bash
> export EPICS_CA_MAX_ARRAY_BYTES=40000000
> ```
> The IOC sets this automatically via the systemd service. Phoebus users should add it to their shell profile and launch Phoebus from Terminal:
> ```bash
> echo 'export EPICS_CA_MAX_ARRAY_BYTES=40000000' >> ~/.zshrc
> source ~/.zshrc
> open -a CSS_Phoebus
> ```

If the client can't find the IOC, see the network troubleshooting section below.

---

## Troubleshooting

```bash
# View live IOC logs
sudo journalctl -u vpcam -f

# Restart the IOC
sudo systemctl restart vpcam

# Check camera is detected
rpicam-hello --list-cameras

# Monitor temperature on the Pi
watch -n 2 vcgencmd measure_temp
```

**CPU temperature too high:**

The CM5 can run hot under continuous camera workloads. The Waveshare Nano B has a 4-pin fan connector — the official Raspberry Pi active cooler or any compatible 5V PWM fan plugged into it will be controlled automatically by the OS thermal governor (no config required). Keep an eye on `cam1:CpuTemp_RBV` via EPICS or the web dashboard. Sustained temperatures above 80 °C will trigger thermal throttling; above 85 °C the Pi may shut down.

**Client can't find the IOC over the network:**

CA uses UDP broadcast for discovery. If your client and Pi are on the same subnet but it still can't connect, set the addr list explicitly on the client machine:

```bash
export EPICS_CA_ADDR_LIST=<Pi IP address>
export EPICS_CA_AUTO_ADDR_LIST=NO
caproto-get VPCAM:01:cam1:Hostname_RBV
```

Find the Pi's IP with `hostname -I` on the Pi, or check your router's DHCP table.
