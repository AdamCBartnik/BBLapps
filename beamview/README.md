# beamview

A live viewer and analysis tool for beam-camera images at **Cornell's Bright
Beams Lab (BBL)** — PyQt5 + pyqtgraph, a Python port of the group's original
MATLAB tool. It shows a camera, fits the spot, and publishes the results back
to EPICS for scan scripts to read.

## Install

Ordinary conda/pip. In miniforge:

```
mamba install -c conda-forge numpy scipy "pyqt=5" pyqtgraph h5py pyyaml pyepics
pip install opencv-python-headless
```

Then clone BBLapps and, from its root, `pip install -e .` — that is only for
`BBL`, which beamview uses for colormaps and snapshot numbering. beamview
itself runs from the checkout and is not installed.

Two things that are easy to get wrong:

- **Pin `pyqt=5`.** beamview imports PyQt5 explicitly, and an unpinned `pyqt`
  can resolve to Qt6.
- **opencv is optional but wanted.** It is the fast median-filter path — about
  2 ms versus 250 ms at 2048×1536. Without it beamview silently falls back to
  scipy and just feels sluggish on big cameras, with nothing to tell you why.

**Big sensors also need `EPICS_CA_MAX_ARRAY_BYTES` raised** on the client. The
symptom is a failed transfer and a timed-out `ca.get` on the image array. Size
it from the *wire* format, not the pixel depth: Channel Access has no unsigned
16-bit type, so Mono16 goes out as 32-bit, and the frame costs
`width × height × 4` bytes — 4024×3036 is about 49 MB. Setting it to
`100000000` covers everything here. On Windows, make it a system environment
variable so it applies to whatever launches beamview.

`vpcam/docs/how_to_install_iocs_on_windows.txt` covers this in more detail,
alongside the camera-side setup.

## Running it

```
python -m beamview.main --config configs/b24.yaml
```

One YAML config per lab area, listing that area's cameras. (There are other
ways to start it — see the end.)

On Windows, `windows_batch/Beamview.bat` is a double-clickable launcher. It
lists every config in `configs/` by its lab name, and closes its console once
you pick one. Copy it wherever you like — Desktop, Start menu — and edit the
block at the top marked `--- edit these ---`:

| Variable | What to set it to |
|---|---|
| `CONDA` | The miniforge/miniconda root. `where conda` in an Anaconda Prompt if unsure — miniforge and miniconda differ |
| `REPO` | The BBLapps checkout |
| `DEFAULT` | Which config Enter picks, e.g. `xlight.yaml`. Blank it (`""`) to make Enter quit instead |
| `DEBUG` | Not per-machine — `1` keeps the console open and shows beamview's output |

Only the first three change per machine. With `DEBUG=0` the launcher goes
through `pythonw.exe` so the console disappears, which also means a crash at
startup leaves no message behind — flip it to `1` first thing when something
won't start.

Drop a new `.yaml` into `configs/` and it appears in the menu automatically.

## Config files

`configs/*.yaml`, one per lab area:

```yaml
name: B29                  # shown in the window title
epics_prefix: "B29"        # area prefix the analysis results publish under
publish_to_epics: true     # optional, default true; the lab-wide "To EPICS" default
cameras:
  - id: "B29Screen1"       # the camera's own areaDetector prefix
  - id: "EMPAD"
    dual: true             # two-image camera -> Normal/Cold/Hot/Diff
```

Note the two namespaces are different things: a camera's `id` is its own
areaDetector prefix, while `epics_prefix` is where *beamview's* analysis
records go.

## What it does

Image handling — hardware ROI, rotation, pixel or calibrated units, frame
averaging with a reset, background capture and subtraction, and a 3×3 median
filter.

Analysis — centroid, widths, super-gaussian fit, threshold with an optional
allow-negative, and a *Brightest box* software ROI that restricts every
analysis to the brightest N×N region.

*Save max value* keeps a per-pixel running maximum — sweep the beam across a
viewscreen and the accumulated map is that screen's sensitivity, which
`bbl.image.screen_sensitivity_correction` then divides out of real images.

Snapshots save a PNG plus an `.h5`, and `bbl.image.get_frame("....h5")` reads
them straight back.

> **The plotted x-axis increases to the LEFT.** This exists so beamview's
> screen frame stays right-handed relative to the accelerator physics
> convention that the solenoid-scan fit assumes.

## EPICS output records

With *Enable Analysis* and *To EPICS* both ticked, beamview publishes six
records each time it analyses a frame. Names are `<epics_prefix>:<record>`,
using the prefix chosen in the GUI — so with `epics_prefix: "B24"` the first
one is `B24:centroid_x`. If no prefix is set, the bare record name is used.

| Record | Meaning |
|---|---|
| `centroid_x` | Horizontal centroid |
| `centroid_y` | Vertical centroid |
| `rms_x` | Horizontal rms width |
| `rms_y` | Vertical rms width |
| `total_intensity` | Sum over the analysed region |
| `peak_intensity` | Brightest single pixel |

Positions and widths are in whatever unit the display is showing — the
camera's calibrated unit normally, or pixels if *Units = pixels* is ticked.
The two intensities are in raw camera counts.

Two things to know when reading these from a scan script:

- **The *Brightest box* selector changes what they mean.** When it is on, the
  frame is masked to that box first, so all six records — including
  `total_intensity` — describe the box rather than the whole frame.
- **Writes are fire-and-forget, and NaNs are skipped.** A record simply holds
  its previous value rather than going stale-flagged, so a scan should read
  back deliberately (`bbl.epics.caget(..., stale=True)`) after changing
  anything upstream.

`publish_to_epics: false` in a config sets the lab-wide default for the *To
EPICS* checkbox — useful where there are no analysis records to write to.

## Architecture

Every camera is served through the same **EPICS areaDetector contract**
(`vpcam/ioc/ad_ioc_base.py`), so beamview needs only one backend,
`cameras/epics_areadetector.py`, and talks to all of them identically. Reads
go through persistent auto-monitored PVs for speed.

Two-image ("double") cameras publish `image1` and `image2` under a shared
`UniqueId`, and beamview forms Normal, Cold, Hot and Diff from the pair.

## Other ways to start it

Occasionally useful, but `--config` is what you normally want:

```
python -m beamview.main --epics VPCAM:03           # one IOC directly, no config file
python -m beamview.main --epics EMPAD --dual       # ...treating it as a two-image camera
```

`--dual` only applies to `--epics`; in a config file it is the per-camera
`dual: true` key instead.
