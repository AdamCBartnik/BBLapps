"""
EPICSAreaDetectorCamera — camera backend for Area Detector IOCs.

PV naming convention (prefix = e.g. "CMM:Screen1" or "VPCAM:01:GB"):

    <prefix>:cam1:GC_ExposureTime      set exposure (seconds)
    <prefix>:cam1:GC_ExposureTime_RBV  readback
    <prefix>:cam1:GC_Gain              set gain
    <prefix>:cam1:GC_Gain_RBV         readback
    <prefix>:cam1:GC_Width_RBV         current frame width
    <prefix>:cam1:GC_Height_RBV        current frame height
    <prefix>:cam1:GC_WidthMax_RBV      sensor max width
    <prefix>:cam1:GC_HeightMax_RBV     sensor max height
    <prefix>:cam1:GC_OffsetX_RBV       ROI offset X
    <prefix>:cam1:GC_OffsetY_RBV       ROI offset Y
    <prefix>:cam1:GC_OffsetX           set ROI offset X
    <prefix>:cam1:GC_OffsetY           set ROI offset Y
    <prefix>:cam1:GC_Width             set ROI width
    <prefix>:cam1:GC_Height            set ROI height
    <prefix>:cam1:n_bits               bits per pixel
    <prefix>:cam1:Acquire              "Acquire" / "Done"
    <prefix>:cam1:FrameRate            set frame rate
    <prefix>:image1:ArrayData          image waveform (uint16, row-major w*h)
    <prefix>:image1:ArrayData_int      monitor PV — fires when new frame ready

New-frame detection uses a CA monitor on ArrayData_int (same pattern as
CameraEPICS.m / wait_for_new_frame).
"""

import os
import numpy as np
import epics

from .base import CameraBase

os.environ.setdefault("EPICS_CA_MAX_ARRAY_BYTES", "40000000")


class EPICSAreaDetectorCamera(CameraBase):
    """
    Camera backend for Area Detector / GC-style EPICS IOCs.

    epics_prefix: prefix WITHOUT trailing colon, e.g. "CMM:Screen1"
    """

    def __init__(self, epics_prefix: str):
        self._prefix = epics_prefix.rstrip(":")

        # Monitor ArrayData_int — fires whenever a new frame is published
        self._new_frame = False
        self._monitor_pv = epics.PV(
            self._prefix + ":image1:ArrayData_int",
            callback=self._on_new_frame,
            auto_monitor=True,
        )

    # ------------------------------------------------------------------
    # CameraBase interface
    # ------------------------------------------------------------------

    @property
    def width(self) -> int:
        return int(self._get(":cam1:GC_Width_RBV") or 0)

    @property
    def height(self) -> int:
        return int(self._get(":cam1:GC_Height_RBV") or 0)

    @property
    def width_max(self) -> int:
        return int(self._get(":cam1:GC_WidthMax_RBV") or self.width)

    @property
    def height_max(self) -> int:
        return int(self._get(":cam1:GC_HeightMax_RBV") or self.height)

    @property
    def offset_x(self) -> int:
        return int(self._get(":cam1:GC_OffsetX_RBV") or 0)

    @property
    def offset_y(self) -> int:
        return int(self._get(":cam1:GC_OffsetY_RBV") or 0)

    @property
    def exposure_time(self) -> float:
        v = self._get(":cam1:GC_ExposureTime_RBV")
        return float(v) if v is not None else 0.0

    @exposure_time.setter
    def exposure_time(self, value: float):
        max_rate = min(10.0, 0.95 / max(value, 1e-6))
        epics.caput(self._prefix + ":cam1:FrameRate", max_rate)
        epics.caput(self._prefix + ":cam1:GC_ExposureTime", value)

    @property
    def gain(self) -> float:
        v = self._get(":cam1:GC_Gain_RBV")
        return float(v) if v is not None else 1.0

    @gain.setter
    def gain(self, value: float):
        epics.caput(self._prefix + ":cam1:GC_Gain", float(value))

    @property
    def bits(self) -> int:
        v = self._get(":cam1:n_bits")
        return int(v) if v is not None else 16

    def snapshot(self) -> np.ndarray:
        w = self.width
        h = self.height
        if w <= 0 or h <= 0:
            return np.zeros((1, 1), dtype=np.uint16)

        raw = epics.caget(
            self._prefix + ":image1:ArrayData",
            count=w * h,
            timeout=5.0,
            as_numpy=True,
        )
        if raw is None:
            return np.zeros((h, w), dtype=np.uint16)

        raw = np.asarray(raw, dtype=np.uint16)
        if raw.size < w * h:
            padded = np.zeros(w * h, dtype=np.uint16)
            padded[: raw.size] = raw
            raw = padded
        # Area Detector stores row-major (h rows × w cols), same as MATLAB reshape(w,h).'
        return raw[: w * h].reshape(h, w)

    def has_new_frame(self) -> bool:
        if self._new_frame:
            self._new_frame = False
            return True
        return False

    def start_streaming(self, rate_hz: float = 5.0):
        epics.caput(self._prefix + ":cam1:Acquire", "Acquire")

    def stop_streaming(self):
        epics.caput(self._prefix + ":cam1:Acquire", "Done")

    def close(self):
        self.stop_streaming()
        self._monitor_pv.disconnect()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get(self, suffix: str, timeout: float = 2.0):
        return epics.caget(self._prefix + suffix, timeout=timeout)

    def _on_new_frame(self, **kwargs):
        self._new_frame = True
