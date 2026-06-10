"""
config_loader.py

Loads a beamview YAML config file and returns a list of CameraEntry objects,
one per camera, in the order they appear in the file.

Beamview connects ONLY to standard areaDetector IOCs (real areaDetector or
the VPCam contract IOCs — Pi cameras, GigE-camera IOCs, CA-relay gateways).
Direct camera connections (old VPCAM private PVs, direct GigE via Harvester)
were removed in the standard-areaDetector migration; run the matching IOC
from the vpcam repo instead.

Camera ID format — every id is simply the IOC's PV prefix:

    "<prefix>"         → EPICSAreaDetectorCamera("<prefix>")
                         e.g. "EMPAD", "VPCAM:03", "VPCAMGW:02"
    "MOCK"             → built-in mock camera (no hardware)

Scale calibration PVs:
    Read/written as  <prefix>:cam1:CalibX  and  <prefix>:cam1:CalibY
    in micrometers per pixel (VPCam extension records; absent on plain
    areaDetector IOCs, in which case the entry is created without EPICS
    calibration and the scale boxes are local-only).

Example YAML
------------
name: B29
epics_prefix: "B29"

cameras:
  - id: "EMPAD"
  - id: "VPCAM:01"
"""

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import re

import epics
import yaml

from .cameras.base import CameraBase


@dataclass
class CameraEntry:
    display_name: str          # shown in the dropdown
    camera: CameraBase         # live camera object
    cal_prefix: str            # prefix used for cam1:CalibX/Y PVs
    has_epics_cal: bool = True # False for mock/non-EPICS cameras


def _strip_trailing_colon(s: str) -> str:
    return s.rstrip(":")


def _load_scale(cal_prefix: str) -> tuple[float, float]:
    """Read cam1:CalibX/Y (um/pixel). Raises RuntimeError if unavailable."""
    px = epics.caget(f"{cal_prefix}:cam1:CalibX", timeout=3.0)
    py = epics.caget(f"{cal_prefix}:cam1:CalibY", timeout=3.0)
    if px is None or py is None:
        missing = []
        if px is None:
            missing.append(f"{cal_prefix}:cam1:CalibX")
        if py is None:
            missing.append(f"{cal_prefix}:cam1:CalibY")
        raise RuntimeError(
            f"Could not read calibration PV(s): {', '.join(missing)}"
        )
    return float(px), float(py)


def _write_scale(cal_prefix: str, x: float, y: float) -> None:
    """Write cam1:CalibX/Y (um/pixel) back to EPICS."""
    epics.caput(f"{cal_prefix}:cam1:CalibX", x)
    epics.caput(f"{cal_prefix}:cam1:CalibY", y)


def _make_camera(camera_id: str) -> tuple[str, CameraBase, str, bool]:
    """
    Parse a camera ID string and return
    (display_name, camera_object, cal_prefix, has_epics_cal).
    """
    if re.match(r"^MOCK$", camera_id, re.IGNORECASE):
        from .cameras.mock import MockCamera
        return "Mock", MockCamera(), "", False

    if re.match(r"^\d+\.\d+\.\d+\.\d+$", camera_id):
        raise ValueError(
            f"Direct GigE connections were removed ({camera_id}). Run a "
            "'gige' type IOC from the vpcam repo on the camera subnet and "
            "use its PV prefix here."
        )

    # Everything else is a standard-areaDetector IOC prefix
    prefix = camera_id

    from .cameras.epics_areadetector import EPICSAreaDetectorCamera
    prefix = _strip_trailing_colon(prefix)
    cam = EPICSAreaDetectorCamera(prefix)
    # CalibX/Y are VPCam extension records; a plain areaDetector IOC won't
    # have them. Probe once so such cameras degrade to local-only scale.
    has_cal = epics.caget(f"{prefix}:cam1:CalibX", timeout=2.0) is not None
    return prefix, cam, prefix, has_cal


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
    """
    yaml_path = Path(yaml_path)
    with open(yaml_path) as fh:
        cfg = yaml.safe_load(fh)

    lab_name = cfg.get("name", yaml_path.stem)
    epics_prefix = cfg.get("epics_prefix", "")
    camera_ids = [c["id"] for c in cfg.get("cameras", [])]

    if not camera_ids:
        raise ValueError(f"No cameras listed in {yaml_path}")

    entries: list[CameraEntry] = []
    errors: list[str] = []

    for cid in camera_ids:
        try:
            display, cam, cal_prefix, has_cal = _make_camera(cid)
            entries.append(CameraEntry(
                display_name=display,
                camera=cam,
                cal_prefix=cal_prefix,
                has_epics_cal=has_cal,
            ))
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
