"""
config_loader.py

Loads a beamview YAML config file and returns a list of CameraEntry objects,
one per camera, in the order they appear in the file.

Camera ID format (mirrors the MATLAB beamview_<lab>.m convention):

    "EPICS:<prefix>"   → EPICSAreaDetectorCamera("<prefix>")
                         display name: "<prefix>"  (EPICS: stripped)
    "VPCAM:<name>"     → VPCAMCamera("VPCAM:<name>")
                         display name: "VPCAM:<name>"
    "192.168.x.x"      → (future GigE — placeholder, raises NotImplementedError)

Scale calibration PVs:
    Read/written as  <cal_prefix>_x_cal  and  <cal_prefix>_y_cal
    where cal_prefix = prefix with trailing colon stripped.

    For EPICS cameras:  cal_prefix = the EPICS prefix (e.g. "CMM:Screen1")
    For VPCAM cameras:  cal_prefix = the VPCAM prefix (e.g. "VPCAM:01:GB")

Example YAML
------------
name: B29
epics_prefix: "B29:"

cameras:
  - id: "EPICS:EMPAD"
  - id: "192.168.128.2"
  - id: "VPCAM:01:GB"
"""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import re

import epics
import yaml

from .cameras.base import CameraBase


@dataclass
class CameraEntry:
    display_name: str          # shown in the dropdown
    camera: CameraBase         # live camera object
    cal_prefix: str            # prefix used for _x_cal / _y_cal PVs
    has_epics_cal: bool = True # False for mock/non-EPICS cameras


def _strip_trailing_colon(s: str) -> str:
    return s.rstrip(":")


def _load_scale(cal_prefix: str) -> tuple[float, float]:
    """Read _x_cal and _y_cal from EPICS.  Raises RuntimeError if unavailable."""
    px = epics.caget(f"{cal_prefix}_x_cal", timeout=3.0)
    py = epics.caget(f"{cal_prefix}_y_cal", timeout=3.0)
    if px is None or py is None:
        missing = []
        if px is None: missing.append(f"{cal_prefix}_x_cal")
        if py is None: missing.append(f"{cal_prefix}_y_cal")
        raise RuntimeError(
            f"Could not read calibration PV(s): {', '.join(missing)}"
        )
    return float(px), float(py)


def _write_scale(cal_prefix: str, x: float, y: float) -> None:
    """Write _x_cal and _y_cal back to EPICS."""
    epics.caput(f"{cal_prefix}_x_cal", x)
    epics.caput(f"{cal_prefix}_y_cal", y)


def _make_camera(camera_id: str, gentl_paths: list | None = None) -> tuple[str, CameraBase, str, bool]:
    """
    Parse a camera ID string and return
    (display_name, camera_object, cal_prefix, has_epics_cal).
    """
    if camera_id.upper().startswith("EPICS:"):
        from .cameras.epics_areadetector import EPICSAreaDetectorCamera
        prefix = camera_id[len("EPICS:"):]
        display = prefix
        cal_prefix = _strip_trailing_colon(prefix)
        cam = EPICSAreaDetectorCamera(prefix)
        return display, cam, cal_prefix, True

    if re.match(r"^VPCAM:", camera_id, re.IGNORECASE):
        from .cameras.vpcam import VPCAMCamera
        prefix = camera_id
        display = prefix
        cal_prefix = _strip_trailing_colon(prefix)
        cam = VPCAMCamera(prefix)
        # Direct VPCAM IOCs don't have _x_cal/_y_cal — those only exist on
        # the gateway IOC (accessed as EPICS:VPCAM:xx:GB in the config)
        return display, cam, cal_prefix, False

    if re.match(r"^MOCK$", camera_id, re.IGNORECASE):
        from .cameras.mock import MockCamera
        cam = MockCamera()
        return "Mock", cam, "", False

    if re.match(r"^\d+\.\d+\.\d+\.\d+$", camera_id):
        from .cameras.gige import GigECamera
        paths = gentl_paths or []
        cam = GigECamera(camera_id, paths)
        cal_prefix = camera_id.replace(".", "_")
        return camera_id, cam, cal_prefix, False

    raise ValueError(f"Unrecognised camera ID format: '{camera_id!r}'")


def load_config(yaml_path: str | Path) -> tuple[str, list[CameraEntry], str]:
    """
    Parse a beamview YAML config file.

    Returns
    -------
    lab_name : str
        Human-readable name for this configuration (used in window title).
    entries : list[CameraEntry]
        One entry per camera, in config-file order.
    epics_prefix : str
        Default EPICS prefix for this lab (e.g. "B29"), or "" if not set.

    Raises RuntimeError if any calibration PV is unreachable.
    """
    yaml_path = Path(yaml_path)
    with open(yaml_path) as fh:
        cfg = yaml.safe_load(fh)

    lab_name = cfg.get("name", yaml_path.stem)
    epics_prefix = cfg.get("epics_prefix", "")
    gentl_paths = cfg.get("gentl_paths", [])
    camera_ids = [c["id"] for c in cfg.get("cameras", [])]

    if not camera_ids:
        raise ValueError(f"No cameras listed in {yaml_path}")

    entries: list[CameraEntry] = []
    errors: list[str] = []

    for cid in camera_ids:
        try:
            display, cam, cal_prefix, has_cal = _make_camera(cid, gentl_paths)
            entries.append(CameraEntry(
                display_name=display,
                camera=cam,
                cal_prefix=cal_prefix,
                has_epics_cal=has_cal,
            ))
        except NotImplementedError as e:
            # Skip unimplemented types with a warning rather than aborting
            print(f"[config] Skipping {cid}: {e}")
        except Exception as e:
            errors.append(f"{cid}: {e}")

    if errors:
        raise RuntimeError(
            "Failed to initialise one or more cameras:\n" +
            "\n".join(f"  • {e}" for e in errors)
        )

    if not entries:
        raise RuntimeError("No usable cameras found in config.")

    return lab_name, entries, epics_prefix
