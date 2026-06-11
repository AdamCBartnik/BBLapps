# VPCam

**VPCam** is a compact, modular viewport camera system for imaging targets visible through vacuum viewports — including scintillator screens, fluorescent targets, beam interceptors, and any in-vacuum object accessible via an optical viewport. Developed by [Xelera Research LLC](https://xelera.io), Ithaca, NY.

The device is plug-and-play: connect to lab Ethernet, configure a single YAML file, and access all controls and image data via EPICS Channel Access PVs.

---

## Hardware

| Component | Part |
|---|---|
| Compute | Raspberry Pi Compute Module 5 (CM5) |
| Baseboard | Waveshare CM5 Nano B |
| Camera | Raspberry Pi Camera Module 3 (IMX708), Global Shutter Camera (IMX296), or compatible |
| LED Controller | Custom PCBA — MMBT2222A NPN + 4.7 kΩ, GPIO BCM 18 (physical pin 12) |
| Enclosure | 3D printed ASA, brass heat-set inserts |
| OS | Raspberry Pi OS Lite 64-bit (Bookworm) |

---

## Software Stack

- **IOC:** [caproto](https://caproto.github.io/caproto/) (Python, EPICS 3 CA compatible)
- **Camera:** [picamera2](https://github.com/raspberrypi/picamera2)
- **GPIO:** pinctrl (via subprocess — no gpiozero dependency)
- **Web UI:** Flask + Pillow — browser dashboard on port 8080
- **Config:** YAML (`/etc/vpcam/config.yaml`)
- **Autostart:** systemd (`vpcam.service`, `vpcam_web.service`)

---

## Quick Start

See [docs/setup.md](docs/setup.md) for the full step-by-step guide. The high-level sequence is:

1. Flash Pi OS Lite 64-bit (Bookworm) to the CM5 eMMC
2. Clone this repo to `/home/pi/vpcam`
3. Run `bash scripts/install.sh` — prompts for camera type, adds the overlay, installs dependencies, copies config, enables services. Reboots if needed.
4. Edit `/etc/vpcam/config.yaml` to set hostname, PV prefix, and network settings
5. `sudo systemctl start vpcam && sudo systemctl start vpcam_web`
6. Verify with `caproto-get VPCAM:01:cam1:Model_RBV` from the Pi
7. Open `http://vpcam-01.local:8080` in a browser for the web dashboard

---

## EPICS PVs

See [docs/pvs.md](docs/pvs.md) for the full PV reference.

Default prefix: `VPCAM:01`

> **Note for large image PVs:** Image frame sizes vary by camera (up to ~34 MB for IMX708 full-res color). Set `EPICS_CA_MAX_ARRAY_BYTES=40000000` on any client machine connecting to image PVs (this is already set in the service on the Pi itself).

---

## File Locations on the Pi

| File | Path |
|---|---|
| IOC entry point (all camera types) | `/home/pi/vpcam/ioc/vpcam_launcher.py` |
| PV contract (standard areaDetector surface) | `/home/pi/vpcam/ioc/ad_ioc_base.py` |
| Camera drivers | `/home/pi/vpcam/ioc/vpcam_drivers.py` |
| Web UI | `/home/pi/vpcam/webui/vpcam_web.py` |
| Config | `/etc/vpcam/config.yaml` |
| IOC Service | `/etc/systemd/system/vpcam.service` |
| Web Service | `/etc/systemd/system/vpcam_web.service` |

## Repository Layout

```
ioc/            caproto IOC — PV server and camera control
webui/          Flask web dashboard (vpcam_web.py) + product page (portview-275.html)
docs/           Setup guide, PV reference, calibration, wiring
phoebus/        Phoebus CS-Studio dashboard (.bob file)
scripts/        install.sh, update.sh, MOTD script
systemd/        systemd service unit files
enclosure/      3D-printable enclosure CAD files (.stp)
```

> `webui/portview-275.html` is a standalone product marketing page for the PortView 275 (the commercial name for this device). It is not served by the web dashboard — open it directly in a browser.

---

## Documentation

- [Setup Guide](docs/setup.md)
- [PV Reference](docs/pvs.md)
- [IMX296 Global Shutter Camera](docs/camera_imx296_gs.md)
- [Calibration](docs/calibration.md)
- [Wiring](docs/wiring.md)

---

## License

See [LICENSE](LICENSE).
