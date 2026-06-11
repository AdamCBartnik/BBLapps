# Raspberry Pi Global Shutter Camera (IMX296)

## Overview

The Raspberry Pi Global Shutter Camera uses the Sony IMX296 sensor — a 1.58 MP
color (Bayer RGGB) image sensor with a **global shutter**.  Unlike the rolling-
shutter IMX708 used in Camera Module 3, every pixel in the IMX296 is exposed and
read out simultaneously, so fast-moving objects and pulsed-light sources appear
without any horizontal tearing or skew.

| Spec | Value |
|---|---|
| Sensor | Sony IMX296 (color, Bayer RGGB) |
| Active pixels | 1456 × 1088 (1.58 MP) |
| Pixel size | 3.45 µm × 3.45 µm |
| Sensor diagonal | 6.3 mm (1/2.9″) |
| Shutter | Global (all pixels read simultaneously) |
| Max frame rate | 60 fps at full resolution |
| Minimum exposure | ~30 µs |
| Lens mount | C-mount (lens not included) |
| Interface | CSI-2 (ribbon cable to Pi) |

**Key differences from Camera Module 3 (IMX708):**

- Global shutter — no rolling-shutter skew for pulsed beams or moving targets
- C-mount interchangeable lenses — no motor-driven autofocus
- Lower resolution (1456×1088 vs 4608×2592)
- Single sensor mode — no 2×2 binning option
- Flip controls (hflip/vflip) require a camera reconfigure (~1 s) because
  libcamera applies them as a Transform at configuration time

---

## Hardware Changes vs. Standard IMX708 Setup

If you are converting an existing vpcam deployment from the IMX708 to the IMX296,
the only hardware change is the camera module itself.  The CSI ribbon connector,
board slot (CAM1 on the Waveshare Nano B), and GPIO/LED wiring are identical.

> **Lens required:** The IMX296 ships without a lens.  You must fit a C-mount or
> CS-mount lens before the camera will produce a usable image.  A CS-to-C adapter
> ring is included with the camera.  Choose a focal length appropriate for your
> working distance (see Lens Selection below).

---

## Software Changes vs. Standard IMX708 Setup

### 1. Device tree overlay

Replace the IMX708 overlay with the IMX296 overlay in `/boot/firmware/config.txt`.

Remove (or comment out):
```
dtoverlay=imx708
```

Add:
```
dtoverlay=imx296,cam1
```

Reboot:
```bash
sudo reboot
```

Verify the camera is detected:
```bash
libcamera-hello --list-cameras
```

Expected output:
```
Available cameras
-----------------
0 : imx296 [1456x1088] (/base/soc/i2c0mux/i2c@1/imx296@1a)
    Modes: 'SRGGB10_CSI2P' : 1456x1088 [60.38 fps - (0, 0)/1456x1088 crop]
```

If the camera is not listed, check:
- The CSI ribbon is fully seated at both ends
- You are using CAM1, not CAM0, on the Waveshare Nano B
- `grep imx296 /boot/firmware/config.txt` shows the overlay
- `sudo dmesg | grep imx296` for driver errors

Run a quick image capture to confirm the full pipeline works:
```bash
libcamera-still -o /tmp/test_imx296.jpg
ls -lh /tmp/test_imx296.jpg   # should be ~1–2 MB
```

### 2. Config file

Copy the IMX296 config template:
```bash
sudo cp /home/pi/vpcam/ioc/config_imx296.yaml.example /etc/vpcam/config.yaml
```

Edit key fields:
```bash
sudo nano /etc/vpcam/config.yaml
```

| Field | IMX296 value | Notes |
|---|---|---|
| `epics.prefix` | `VPCAM:01` (suggested) | Change to suit your naming |
| `roi.width` | `1456` | Full sensor width |
| `roi.height` | `1088` | Full sensor height |
| `camera.exposure_time_us` | `5000` | Starting point; adjust for scene |
| `camera.hflip` / `vflip` | `0` | Set 1 if image is mirrored |

> Do not set `roi.width` > 1456 or `roi.height` > 1088.  These determine the
> maximum PV array size at startup.

### 3. IOC file

All camera types share one entry point — `vpcam_launcher.py` reads
`camera.type` from config.yaml and loads the right driver.

If running manually:
```bash
python /home/pi/vpcam/ioc/vpcam_launcher.py
```

The systemd service already points at it; after config changes just reload
and restart:
```bash
sudo systemctl daemon-reload
sudo systemctl restart vpcam
sudo systemctl status vpcam
```

### 4. EPICS_CA_MAX_ARRAY_BYTES

At full 1456×1088 resolution `image1:ArrayData` is ~3.2 MB (uint16).
Set this on any client machine before connecting:

```bash
export EPICS_CA_MAX_ARRAY_BYTES=40000000
```

The IOC sets this automatically via systemd.

---

## PV Reference

All cameras serve the same standard-areaDetector PV surface — see
[pvs.md](pvs.md) for the complete reference. Notes specific to the IMX296:

- The image is the Bayer-block sum R+G1+G2+B of 10-bit raw values, published
  as uint16 (`cam1:BitsPerPixel_RBV` = 10 as a per-channel saturation bound).
- `cam1:HFlip` / `cam1:VFlip` reconfigure the camera (~1 s).
- IMX708-only extensions (`cam1:AfMode`, `cam1:LensPosition`,
  `cam1:SensorMode`, image-quality controls) are absent: the IMX296 has no
  VCM autofocus (focus is mechanical on the C-mount lens) and exposes a
  single sensor mode (1456×1088).

---

## Lens Selection

The IMX296 uses a C-mount lens (the same standard used on machine-vision cameras).
A CS-to-C adapter ring is included.  Choose focal length based on your working
distance and desired field of view.

The sensor is 6.3 mm diagonal (1456 × 1088 px × 3.45 µm/px).

| Working distance | Recommended focal length | ~FOV (H × V) |
|---|---|---|
| 100 mm | 6 mm | 93 × 70 mm |
| 200 mm | 12 mm | 93 × 70 mm |
| 300 mm | 16 mm | 90 × 68 mm |
| 500 mm | 25 mm | 91 × 68 mm |
| 1000 mm | 50 mm | 91 × 68 mm |

> These are approximations for a thin-lens model.  Verify field of view with a
> calibration target before recording calibration constants in `config.yaml`.

---

## Testing Procedure

### 1. Verify camera detection
```bash
libcamera-hello --list-cameras
# Expect: imx296 [1456x1088]
```

### 2. Start IOC and confirm PVs register
```bash
python /home/pi/vpcam/ioc/vpcam_launcher.py --list-pvs 2>&1 | head -60
```

### 3. Check system PVs from a client machine
```bash
export EPICS_CA_MAX_ARRAY_BYTES=40000000
caproto-get VPCAM:01:cam1:Model_RBV
caproto-get VPCAM:01:cam1:CpuTemp_RBV
```

### 4. Trigger a frame and read dimensions
```bash
caproto-put VPCAM:01:cam1:ImageMode Single
caproto-put VPCAM:01:cam1:Acquire Acquire
caproto-get VPCAM:01:image1:ArraySize0_RBV
caproto-get VPCAM:01:image1:ArraySize1_RBV
# Expect: 1456, 1088 (or ROI values if configured)
```

### 5. Test manual exposure
```bash
caproto-put VPCAM:01:cam1:AeEnable 0
caproto-put VPCAM:01:cam1:AcquireTime 0.01    # seconds
caproto-put VPCAM:01:cam1:Gain 2.0
caproto-put VPCAM:01:cam1:Acquire Acquire
```

### 6. Test flip controls
```bash
caproto-put VPCAM:01:cam1:HFlip 1   # image should flip horizontally (~1 s delay)
caproto-put VPCAM:01:cam1:HFlip 0   # restore
caproto-put VPCAM:01:cam1:VFlip 1   # image should flip vertically
caproto-put VPCAM:01:cam1:VFlip 0   # restore
```

### 7. Test continuous capture via web UI
Open `http://<Pi IP>:8080` in a browser.  The live image should update at the
configured `autotrigger_rate_hz` (or after pressing Acquire if booted idle).

### 8. Test ROI (writes apply immediately, driver clamps)
```bash
caproto-put VPCAM:01:cam1:MinX 100
caproto-put VPCAM:01:cam1:MinY 100
caproto-put VPCAM:01:cam1:SizeX 800
caproto-put VPCAM:01:cam1:SizeY 600
caproto-get VPCAM:01:cam1:SizeX_RBV      # expect 800
# Restore full frame: zero offsets, oversize dims (clamped to max)
caproto-put VPCAM:01:cam1:MinX 0
caproto-put VPCAM:01:cam1:MinY 0
caproto-put VPCAM:01:cam1:SizeX 99999
caproto-put VPCAM:01:cam1:SizeY 99999
```

---

## Troubleshooting

**Camera not detected (`libcamera-hello` shows no cameras):**
- Confirm `dtoverlay=imx296,cam1` is in `/boot/firmware/config.txt`
- Reboot after adding the overlay
- Check ribbon cable at both ends
- `sudo dmesg | grep -i imx296` for driver errors
- Confirm you are using CAM1 (not CAM0) on the Waveshare Nano B

**Image is completely black:**
- Lens cap still on
- Exposure time too short for available light — try `cam1:AeEnable=1` first to
  get auto-exposed frames, then switch to manual once you have a reference value
- Confirm LED is on if illuminating the scene via LED: `caproto-put VPCAM:01:cam1:LedEnable 1`

**Image is blurry:**
- Focus is set mechanically on the C-mount lens barrel — adjust the focus ring
- Verify working distance matches lens focal length

**HFlip/VFlip write returns immediately but image has not changed:**
- The reconfigure takes ~1 s; wait for the next frame to arrive

**ROI seems to ignore my write:**
- ROI records apply immediately and the driver clamps out-of-range values —
  read the `_RBV` records to see what was actually applied. The ArrayData
  buffer is sized to the full sensor at startup, so any in-range ROI works
  without a restart.
