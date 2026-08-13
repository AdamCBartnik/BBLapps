# BBLapps — orientation for AI assistants / developers

Local working copy of the **BBLapps** GitHub repo (`AdamCBartnik/BBLapps`).
BBL = Bright Beams Lab, Cornell (synchrotron beamline). This file is a quick
orientation; **git history is the source of truth** for what changed.

## Top-level layout
- `beamview/` — PyQt5 + pyqtgraph GUI for viewing/analyzing beam-camera images
  (Python port of a MATLAB tool). Entry: `python -m beamview --config <yaml> | --epics <PREFIX> | --mock`.
- `vpcam/ioc/` — camera IOCs (caproto) serving a standard **EPICS areaDetector**
  PV contract. `ad_ioc_base.py` is the one shared contract module; per-backend
  drivers sit on top. Standalone tools: `mock_ioc.py`, `gateway_ioc.py`,
  `aravis_ioc.py` (all GigE cameras), plus `vpcam_launcher.py` (Pi cameras).
  Retired code lives in `vpcam/ioc/attic/` (e.g. the old vendor-GenTL
  `gige_ioc.py`).
- `EMPAD/` — the EMPAD detector's IOC (electron detector, two-image pump/probe).
  `scripts/empad_ioc.py` (new areaDetector-style IOC) + `scripts/python_ioc.py`
  (camserver/trigger controller). Originals in `scripts/original_version/`.
- `BBL/` — shared Python package for scripts/notebooks (`import BBL as bbl`),
  organised into subpackages the way scipy is (reorganised 2026-08; there
  are NO flat aliases, `bbl.caget` is gone):

      bbl.epics       caget, caput, restore_pvs
      bbl.plot        LivePlot, warmup, get_colormap
      bbl.image       get_frame, plot_frame, screen_sensitivity_correction
      bbl.utilities   polyfit_weights, get_todays_directory, ssss,
                      next_ssss_stem, measure_trend
      bbl.gun         center_laser_in_gun, fit_gun_aberration
      bbl.solenoid    solenoid_scan, fit_solenoid_scan
      bbl.fieldmaps   load_onaxis_field  (+ the .gdf maps, gitignored)
      bbl.cnf         model_qe_map, patterns  (CNF photocathode masks)

  `_physics.py` is private and shared by gun/solenoid. Nothing is imported
  until touched, so `import BBL` stays cheap and a broken optional dep can
  only break the subpackage that needs it. (matplotlib is NOT optional —
  it is a core dependency, and the lab machines have it. `get_colormap`
  returns a matplotlib `Colormap`; pass `return_list=True` for the raw
  (m, 3) array, which is what beamview feeds to pyqtgraph.)
  **Naming rule: a module is named for its SUBJECT, never for a function it
  contains.** A module sharing a name with a function it exports shadows
  that function once imported — that shipped as a real bug twice before the
  reorganisation (hence `frames.py` not `get_frame.py`, `saving.py` not
  `ssss.py`). Subpackage names are domains, so they can't collide.
  Was `utilities/` before 2026-07, flat until 2026-08.
  Pip-installable via the root `pyproject.toml`: `pip install -e .` makes
  `import BBL` work from any directory (done on this machine); beamview and
  the IOCs are NOT pip-installed — they run from a checkout / copied files.
- `matlab_code/` — original MATLAB reference (untracked; reference only).
  Scan scripts (`center_laser_in_gun/`, `solenoid/`, `utilities/measure_trend/`)
  are being ported into `BBL/` and adapted to the current accelerator.

## Architecture in one line
Every camera is served through the **same areaDetector contract**
(`vpcam/ioc/ad_ioc_base.py`), so beamview's single backend
(`beamview/cameras/epics_areadetector.py`) talks to all of them identically —
Pi cameras, GigE, the CA gateway, the mock, and EMPAD.

Two-image ("double") cameras publish `image1`+`image2` (shared `UniqueId`);
beamview forms Normal/Cold/Hot/Diff. Declared per-camera via `dual: true` in the
config (not probed). Reads use persistent auto-monitored PVs for speed.

## Dev quickstart (this machine)
- Python: `C:\ProgramData\miniforge3\python.exe` (NOT on PATH).
- Mock camera for UI work: run `python vpcam/ioc/mock_ioc.py` (serves prefix
  `MOCK`, dual-frame 1000x1000), then `python -m beamview --mock`.
- Only run ONE mock IOC at a time (Windows SO_REUSEADDR lets two bind 5064 and
  they interfere).
- Tests: `beamview/test_ad_backend.py`, `vpcam/ioc/test_relay_chain.py`
  (each spawns its own IOC).

## Current state (2026-07-24)
- Dual-frame two-image support, standalone `mock_ioc.py`, config-driven
  `dual`/`publish_to_epics`, and a cached-monitor read speedup: DONE, pushed.
- **EMPAD rewrite: pushed, NOT yet validated on-site.** The camserver/trigger
  half (`python_ioc.py`) can only be tested on the EMPAD box. See the EMPAD
  memory for the open items (montage→image3 deferred; on-site test pending).
- Deploying an IOC = copy its file(s) + `ad_ioc_base.py` to the target machine.
- **Beamview's plotted x-axis increases to the LEFT** (matches the old
  MATLAB convention; the rendered image itself is unchanged). This exists
  so beamview's screen frame stays right-handed relative to the accelerator
  physics convention the solenoid-scan fit assumes — see the beamview
  memory for the mechanism and why. `BBL/image/frames.py`'s `xx` matches
  (negated); `plot_frame()` needed no changes.
- **`bbl.cnf.model_qe_map` (new 2026-08)** models a lithographic mask seen
  through a gaussian laser spot. It is a Fourier method — every primitive
  has a closed-form transform, so nothing is rasterised and sub-pixel
  features stay exact. Patterns are passed in as data (`bbl.cnf.patterns`
  holds the five ported MATLAB masks), not hard-coded as in the MATLAB
  original. Validated against the exact pixel-averaged erf product for a
  blurred rectangle: `python BBL/cnf/test_qe_map.py`.

## Detailed assistant memory
Richer context (decisions, history, hard-won gotchas) is in the machine-local
auto-memory at `~/.claude/projects/<this-project>/memory/` — not in this repo.
Key files: `project_beamview.md`, `project_bbl_package.md`,
`project_empad_ioc_rewrite.md`, `user_profile.md`.
