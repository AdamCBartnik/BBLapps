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

**Big sensors also need `EPICS_CA_MAX_ARRAY_BYTES` set** on the client. The
symptom is a failed transfer and a timed-out `ca.get` on the image array. Setting it to
`100000000` is sufficient for up to ~4000x3000 pixel cameras. On Windows, it is a system environment
variable.

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

Basic image processing — hardware/software ROI, rotation, frame
averaging, background subtraction, super-gaussian blur, thresholding, 
and a median filter.

Analysis — centroids, widths, integrated and peak intensity. These values are 
then sent to EPICS records.

Snapshots save a PNG plus an `.h5`, which can be loaded back using
`bbl.image.get_frame("....h5")`.

*Brightest box* is software ROI that moves a rectangular ROI to the current 
brightest region per frame. Sometimes simpler to use than specifying a static ROI.

*Save max value* keeps a per-pixel running maximum

> **Note: the plotted x-axis increases to the LEFT.** This exists so beamview's
> screen frame make a right-handed coordinate system with the beam traveling in +z.

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

`publish_to_epics: false` in a config sets the *To
EPICS* checkbox to false— useful when on a system without EPICS.

## Architecture

Every camera is served through the same **EPICS areaDetector contract**
(`vpcam/ioc/ad_ioc_base.py`), so beamview needs only one backend,
`cameras/epics_areadetector.py`, and talks to all of them identically. Reads
go through persistent auto-monitored PVs for speed.

Two-image ("double") cameras (e.g. the EMPAD) publish `image1` and `image2` under a shared
`UniqueId`, and beamview forms Normal, Cold, Hot and Diff from the pair.
