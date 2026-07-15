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
  `gige_ioc.py`, `aravis_ioc.py`, plus `vpcam_launcher.py` (Pi cameras).
- `EMPAD/` — the EMPAD detector's IOC (electron detector, two-image pump/probe).
  `scripts/empad_ioc.py` (new areaDetector-style IOC) + `scripts/python_ioc.py`
  (camserver/trigger controller). Originals in `scripts/original_version/`.
- `BBL/` — shared Python package for scripts/notebooks (`import BBL as bbl`):
  `pv_tools.py` (cached monitored PVs, `get_pv_avg`, `restore_pvs`),
  `live_plot.py` (`LivePlot` for ipympl/JupyterLab live plots), `fitting.py`
  (`polyfit_weights`), `measure_trend.py` (first scan script), `today.py`
  (data-dir logic), `get_colormap.py`. Lazy `__init__` — importing one
  helper doesn't drag in matplotlib/pyepics. Was `utilities/` before 2026-07.
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

## Current state (2026-06-16)
- Dual-frame two-image support, standalone `mock_ioc.py`, config-driven
  `dual`/`publish_to_epics`, and a cached-monitor read speedup: DONE, pushed.
- **EMPAD rewrite: pushed, NOT yet validated on-site.** The camserver/trigger
  half (`python_ioc.py`) can only be tested on the EMPAD box. See the EMPAD
  memory for the open items (montage→image3 deferred; on-site test pending).
- Deploying an IOC = copy its file(s) + `ad_ioc_base.py` to the target machine.

## Detailed assistant memory
Richer context (decisions, history, hard-won gotchas) is in the machine-local
auto-memory at `~/.claude/projects/<this-project>/memory/` — not in this repo.
Key files: `project_beamview.md`, `project_empad_ioc_rewrite.md`, `user_profile.md`.
