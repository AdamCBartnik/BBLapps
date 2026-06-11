# VPCam Optical Calibration

Calibration determines the physical scale of the image — how many millimeters of scintillator surface each pixel represents. This converts pixel measurements (beam spot size, centroid position) into physical units.

The result is two values stored in `/etc/vpcam/config.yaml`:

```yaml
calibration:
  x_mm_per_pixel: 0.050   # example — update with your measured values
  y_mm_per_pixel: 0.050
```

These are published as writable PVs in **µm per pixel** (note the unit
difference from the config file, which stores mm/px):
- `VPCAM:01:cam1:CalibX`
- `VPCAM:01:cam1:CalibY`

Writing a new value takes effect immediately and is persisted to `config.yaml`
automatically (converted to mm/px) — no restart required.

---

## When to Calibrate

- At first installation, once the working distance is finalized
- Any time the lens position (`LensPosition` PV or `config.yaml`) changes
- Any time the camera or enclosure is repositioned relative to the scintillator

---

## What You Need

- A precision reference target placed **in the plane of the scintillator face** — options include:
  - A machined part with a known dimension (e.g. a ground dowel pin of known diameter)
  - A steel rule or precision scale
  - A printed calibration grid (less accurate but workable)
- The VPCam IOC running and reachable via Phoebus or caproto

---

## Axis Convention

Throughout VPCam, **X is the horizontal axis** (left–right in the image) and **Y is the vertical axis** (top–bottom). This matches the ROI records (`MinX`/`SizeX` horizontal, `MinY`/`SizeY` vertical) and the `CalibX` / `CalibY` PVs. Keep this in mind when placing your reference target — measure a horizontal span for X and a vertical span for Y.

---

## Geometry Note (Important)

The scintillator is tilted at **45° to the camera axis**. This means the image is a foreshortened view — the axis along the tilt direction will be compressed by a factor of cos(45°) ≈ 0.707 compared to the axis perpendicular to the tilt.

**Practical implication:** calibrate with a reference target placed directly on the scintillator face (or in the same plane, at the same tilt), not flat-on to the camera. If you calibrate with a flat target perpendicular to the camera axis, your Y scale (along the tilt axis) will be off by ~41%.

If beam spot roundness matters to your customer, both axes need to be calibrated independently.

---

## Procedure

### 1. Set the Working Distance

Confirm the lens position is set to your operational value:

```bash
caproto-get VPCAM:01:cam1:LensPosition
```

If it needs adjustment, write the diopter value (must be in `AfMode = 0`):

```bash
caproto-put VPCAM:01:cam1:AfMode 0
caproto-put VPCAM:01:cam1:LensPosition 4.3
```

### 2. Place the Reference Target

Place your reference target on or immediately in front of the scintillator face, oriented in the same plane as the scintillator. Ensure it is well-lit — turn on the LED:

```bash
caproto-put VPCAM:01:cam1:LedEnable 1
```

### 3. Capture a Frame

Trigger a single frame:

```bash
caproto-put VPCAM:01:cam1:ImageMode Single
caproto-put VPCAM:01:cam1:Acquire Acquire
```

Or use the **Single** button in the Phoebus dashboard.

### 4. Measure Pixels

Open the image in Phoebus. Using the image display, identify two points on your reference target that span a **known physical distance** in X, and separately in Y.

Count the pixel span between those two points using the Phoebus cursor readout.

### 5. Calculate µm/pixel

```
x_um_per_pixel = 1000 × known_distance_x_mm / pixel_span_x
y_um_per_pixel = 1000 × known_distance_y_mm / pixel_span_y
```

**Example:** if a 10mm reference spans 210 pixels in X:
```
x_um_per_pixel = 1000 × 10.0 / 210 = 47.6 µm/px
```

### 6. Write the Calibration PVs

Write the measured values (in µm/px) directly — no restart required:

```bash
caproto-put VPCAM:01:cam1:CalibX 47.6
caproto-put VPCAM:01:cam1:CalibY 47.6
```

The values take effect immediately and are persisted to `config.yaml`
automatically (stored there as mm/px).

### 7. Verify

```bash
caproto-get VPCAM:01:cam1:CalibX
caproto-get VPCAM:01:cam1:CalibY
```

---

## Recording Calibration Data

Document the following each time you calibrate and commit it to the repo or keep it with the device logbook:

| Field | Value |
|---|---|
| Date | |
| Operator | |
| Working distance (mm) | |
| Lens position (diopters) | |
| Reference target used | |
| Pixel span X (px) | |
| Known distance X (mm) | |
| x_mm_per_pixel | |
| Pixel span Y (px) | |
| Known distance Y (mm) | |
| y_mm_per_pixel | |
| Notes | |

---

## Effect of Sensor Mode (Binning) on Calibration

If you change `sensor_mode` in config.yaml from full resolution (mode 0, 4608×2592) to 2×2 binned (mode 1, 2304×1296), each pixel represents twice the physical area. You do not need to recalibrate from scratch — simply multiply your existing values by 2:

```
x_mm_per_pixel (binned) = x_mm_per_pixel (full) × 2
y_mm_per_pixel (binned) = y_mm_per_pixel (full) × 2
```

Update config.yaml and restart the IOC after changing sensor mode.

---

## Known Limitations

- Calibration is valid only at the working distance it was performed at. If the lens position changes, recalibrate.
- The 45° scintillator angle means X and Y scales may differ. Measure both independently.
- There is no distortion correction in the current software. For a well-chosen lens at the working distances used, barrel/pincushion distortion should be negligible, but this has not been verified.
- Calibration values are static — they do not update automatically if the lens refocuses. `AfMode` should remain `0` (manual) during operation.
