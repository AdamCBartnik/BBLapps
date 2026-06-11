# VPCam PV Reference

Default prefix: `VPCAM:01` (set in `/etc/vpcam/config.yaml`)

All PVs use EPICS Channel Access (CA). The full PV name is `<prefix>:<suffix>`,
e.g. `VPCAM:01:cam1:Acquire`.

The PV surface is modeled on the genuine EPICS **areaDetector** record set
(ADCore's ADDriver + NDStdArrays), so any standard areaDetector client works
against a VPCam IOC unmodified. House extensions (LED, lens, system info, …)
live alongside the standard records under `cam1:` and are marked
**EXTENSION** below. The surface is defined in one place:
`ioc/ad_ioc_base.py` (the contract module).

**Acronyms used in this document:**

| Acronym | Meaning |
|---|---|
| PV | Process Variable — a named data channel in EPICS |
| CA | Channel Access — the EPICS 3 network protocol |
| IOC | Input/Output Controller — the server process that hosts PVs |
| ROI | Region of Interest — the cropped sub-region of the sensor used for image output |
| AE | Auto Exposure — automatic control of exposure time by the camera |
| AF | Autofocus — automatic control of lens position by the camera |
| RW | Read/Write — PV can be read and written by a client |
| RO | Read-Only — PV can only be read; writes are rejected |
| `_RBV` | Read-Back Value — reflects the currently active value, not the pending setpoint |

---

## Standard areaDetector records (all cameras)

### Acquisition — `cam1:`

| PV Suffix | R/W | Type | Description |
|---|---|---|---|
| `cam1:Acquire` | RW | enum | `Done` (0) / `Acquire` (1) — start/stop acquisition |
| `cam1:Acquire_RBV` | RO | enum | Acquisition state readback |
| `cam1:ImageMode` (+`_RBV`) | RW | enum | `Single` (0) / `Multiple` (1) / `Continuous` (2) |
| `cam1:NumImages` (+`_RBV`) | RW | int | Frames per Acquire in Multiple mode |
| `cam1:AcquireTime` (+`_RBV`) | RW | float | Exposure time in **seconds** |
| `cam1:AcquirePeriod` (+`_RBV`) | RW | float | Seconds per frame in Continuous mode |
| `cam1:Gain` (+`_RBV`) | RW | float | Analogue gain |
| `cam1:ArrayCounter` (+`_RBV`) | RW | int | Frame counter; write 0 to reset |
| `cam1:ArrayRate_RBV` | RO | float | Acquisition rate (Hz), smoothed |

> ⚠️ `AcquireTime` and `Gain` writes are ignored while `cam1:AeEnable = 1`
> (auto exposure). Set `AeEnable = 0` first.

### ROI — `cam1:`

Writes apply **immediately** (no staged apply step). The driver clamps
out-of-range values; the `_RBV` records show what was actually applied.
The ROI is a software crop on the full-sensor capture.

| PV Suffix | R/W | Type | Description |
|---|---|---|---|
| `cam1:MinX` (+`_RBV`) | RW | int | ROI X offset (px from left) |
| `cam1:MinY` (+`_RBV`) | RW | int | ROI Y offset (px from top) |
| `cam1:SizeX` (+`_RBV`) | RW | int | ROI width (px) |
| `cam1:SizeY` (+`_RBV`) | RW | int | ROI height (px) |
| `cam1:MaxSizeX_RBV` | RO | int | Full sensor width (px) |
| `cam1:MaxSizeY_RBV` | RO | int | Full sensor height (px) |
| `cam1:ArraySizeX_RBV` | RO | int | Current frame width (px) |
| `cam1:ArraySizeY_RBV` | RO | int | Current frame height (px) |

> To reset the ROI to the full sensor, write `MinX=0`, `MinY=0`, then
> oversized `SizeX`/`SizeY` (e.g. 99999) — the driver clamps to the maximum.

### Format / identity — `cam1:`

| PV Suffix | R/W | Type | Description |
|---|---|---|---|
| `cam1:DataType` (+`_RBV`) | RW* | enum | Array data type (`UInt16`); driver-determined, writes reflected back |
| `cam1:ColorMode` (+`_RBV`) | RW* | enum | `Mono`; driver-determined |
| `cam1:Manufacturer_RBV` | RO | string | e.g. `Raspberry Pi` |
| `cam1:Model_RBV` | RO | string | e.g. `Global Shutter Camera (IMX296)` |

### Image data — `image1:`

Image data is a flat waveform with fixed element count
(`MaxSizeX × MaxSizeY`, matching genuine NDStdArrays behavior). Only the
first `ArraySize0_RBV × ArraySize1_RBV` elements are active; read with a
counted get (`count = w*h`).

| PV Suffix | R/W | Type | Description |
|---|---|---|---|
| `image1:ArrayData` | RO | uint16[] | Image waveform, row-major |
| `image1:ArrayCounter_RBV` | RO | int | Frames published — **monitor this PV for new-frame detection**; written last per frame so all metadata is consistent when it fires |
| `image1:UniqueId_RBV` | RO | int | Frame id (gaps ⇒ dropped frames) |
| `image1:TimeStamp_RBV` | RO | float | Unix time of frame capture |
| `image1:ArraySize0_RBV` | RO | int | Active frame width (px) |
| `image1:ArraySize1_RBV` | RO | int | Active frame height (px) |
| `image1:NDimensions_RBV` | RO | int | Always 2 |
| `image1:DataType_RBV` / `ColorMode_RBV` | RO | enum | Mirror of `cam1:` values |

---

## House extension records (all cameras) — EXTENSION

| PV Suffix | R/W | Type | Description |
|---|---|---|---|
| `cam1:BitsPerPixel_RBV` | RO | int | True sensor bit depth in the 16-bit container (10 for IMX296/708 Bayer sum bound). areaDetector has no record for this |
| `cam1:CalibX` / `CalibY` | RW | float | Calibration, **µm per pixel**. Persists to config.yaml (stored there as mm/px) |
| `cam1:AeEnable` | RW | int | Auto exposure: 0 = manual, 1 = auto |
| `cam1:HFlip` / `VFlip` | RW | int | Image flips (picamera2 cameras reconfigure, ~1 s) |
| `cam1:LedEnable` | RW | int | Illumination LED on/off |
| `cam1:LedStatus_RBV` | RO | int | LED commanded-state readback (software only — reads 1 even if hardware failed) |
| `cam1:Hostname_RBV` | RO | string | Device hostname |
| `cam1:IpAddr_RBV` | RO | string | Device IP (updated every 30 s) |
| `cam1:Uptime_RBV` | RO | float | IOC uptime in seconds (every 5 s) |
| `cam1:CpuTemp_RBV` | RO | float | CPU temperature °C (every 10 s) |

---

## IMX708-only extensions — EXTENSION

| PV Suffix | R/W | Type | Description |
|---|---|---|---|
| `cam1:SensorMode` | RW | int | 0 = Full (4608×2592), 1 = 2×2 Binned (2304×1296). Applies live (reconfigure), resets ROI to the new full frame |
| `cam1:AfMode` | RW | int | 0 = Manual, 1 = Auto, 2 = Continuous |
| `cam1:LensPosition` | RW | float | Diopters (0 = infinity, ~4.3 = 23 cm). Writable only when `AfMode = 0` |
| `cam1:Brightness` | RW | float | −1.0 to 1.0 (default 0.0) |
| `cam1:Contrast` | RW | float | 0.0 to 32.0 (default 1.0) |
| `cam1:Sharpness` | RW | float | 0.0 to 16.0 (default 1.0, 0 = off) |
| `cam1:NoiseReductionMode` | RW | int | 0 = Off, 1 = Fast, 2 = High Quality |

> Changing sensor mode changes the effective µm/pixel. In binned mode (1),
> multiply calibration values by 2.

---

## Quick Reference

```bash
# Identity / health
caproto-get VPCAM:01:cam1:Model_RBV
caproto-get VPCAM:01:cam1:Hostname_RBV
caproto-get VPCAM:01:cam1:CpuTemp_RBV

# Exposure (manual)
caproto-put VPCAM:01:cam1:AeEnable 0
caproto-put VPCAM:01:cam1:AcquireTime 0.02      # seconds
caproto-put VPCAM:01:cam1:Gain 1.5

# Continuous acquisition at 5 Hz
caproto-put VPCAM:01:cam1:AcquirePeriod 0.2
caproto-put VPCAM:01:cam1:ImageMode Continuous
caproto-put VPCAM:01:cam1:Acquire Acquire

# Single frame
caproto-put VPCAM:01:cam1:ImageMode Single
caproto-put VPCAM:01:cam1:Acquire Acquire

# Stop
caproto-put VPCAM:01:cam1:Acquire Done

# ROI (applies immediately, clamped by driver)
caproto-put VPCAM:01:cam1:MinX 100
caproto-put VPCAM:01:cam1:MinY 50
caproto-put VPCAM:01:cam1:SizeX 800
caproto-put VPCAM:01:cam1:SizeY 600

# LED
caproto-put VPCAM:01:cam1:LedEnable 1

# Watch for new frames
caproto-monitor VPCAM:01:image1:ArrayCounter_RBV
```

---

## Notes

- `image1:ArrayData` element count is fixed at IOC startup to the largest
  supported frame (`MaxSizeX × MaxSizeY` of the largest sensor mode).
  ROI changes at runtime always fit within this buffer — no restart needed.
- New-frame detection: monitor `image1:ArrayCounter_RBV`, then do a counted
  read of `image1:ArrayData` with `count = ArraySize0_RBV × ArraySize1_RBV`.
- On IMX296 (mono and color) raw values are 10-bit linear; on the color
  cameras the published frame is the Bayer-block sum R+G1+G2+B, so values
  can exceed 1023 — `BitsPerPixel_RBV = 10` is a conservative
  saturation/linearity bound for a single channel.
- EPICS CA has a default maximum array size of 16 KB. Clients reading image
  PVs must set `EPICS_CA_MAX_ARRAY_BYTES=40000000` before connecting.
- Acquisition starts automatically at boot if `camera.autotrigger_rate_hz > 0`
  in config.yaml; set it to 0 to boot idle.
- For camera-specific details see [camera_imx296_gs.md](camera_imx296_gs.md) (IMX296).
