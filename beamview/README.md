# beamview

A live viewer and analysis tool for beam-camera images at **Cornell's Bright
Beams Lab (BBL)** — PyQt5 + pyqtgraph, a Python port of the group's original
MATLAB tool. It shows a camera, fits the spot, and publishes the results back
to EPICS for scan scripts to read.

## Running it

```
python -m beamview.main --config configs/b24.yaml   # a lab's camera list
python -m beamview.main --epics VPCAM:03            # one IOC directly
python -m beamview.main --mock                      # the mock camera
```

Exactly one of those three is required. `--dual` goes with `--epics` to treat
the IOC as a two-image detector. `--mock` expects `mock_ioc.py` from
`vpcam/ioc` to already be running — start it yourself first.

On Windows, `windows_batch/Beamview.bat` gives you a double-clickable launcher
that lists the available configs and closes its console once you pick one.

## Config files

`configs/*.yaml`, one per lab area:

```yaml
name: B29                  # shown in the window title
epics_prefix: "B29"        # area prefix the analysis results publish under
publish_to_epics: true     # optional; false to skip the analysis caputs
cameras:
  - id: "B29Screen1"       # the camera's own areaDetector prefix
  - id: "EMPAD"
    dual: true             # two-image camera → Normal/Cold/Hot/Diff
```

`dual` is declared, not probed. Note the two namespaces are different things:
a camera's `id` is its own areaDetector prefix, while `epics_prefix` is where
*beamview's* analysis records go — `bbl.solenoid.solenoid_scan` wants the
latter, `bbl.image.get_frame` the former.

## What it does

Image handling — hardware ROI, rotation, pixel or calibrated units, frame
averaging with a reset, background capture and subtraction, and a 3×3 median
filter.

Analysis — centroid, widths, super-gaussian fit, threshold with an optional
allow-negative, and a *Brightest box* software ROI that restricts every
analysis to the brightest N×N region. Results publish to EPICS as
`<epics_prefix>:centroid_x` and friends when *To EPICS* is on.

*Save max value* keeps a per-pixel running maximum — sweep the beam across a
viewscreen and the accumulated map is that screen's sensitivity, which
`bbl.image.screen_sensitivity_correction` then divides out of real images.

Snapshots save a PNG plus an `.h5` through the same "see something, save
something" numbering the notebooks use, so `bbl.image.get_frame("….h5")`
reads them straight back.

> **The plotted x-axis increases to the LEFT.** This matches the old MATLAB
> convention; the rendered image itself is unchanged. It exists so beamview's
> screen frame stays right-handed relative to the accelerator physics
> convention that the solenoid-scan fit assumes. `BBL/image/frames.py`
> negates its `xx` to match.

## Architecture

Every camera — Raspberry Pi, GigE, the CA gateway, EMPAD, the mock — is served
through the same **EPICS areaDetector contract** (`vpcam/ioc/ad_ioc_base.py`),
so beamview needs only one backend, `cameras/epics_areadetector.py`, and talks
to all of them identically. Reads go through persistent auto-monitored PVs for
speed.

beamview is run from a checkout, not pip-installed. It uses `BBL` for snapshot
numbering and colormaps, so `pip install -e .` from the repo root once.

## Tests

```
python beamview/test_ad_backend.py     # spawns its own IOC
```
