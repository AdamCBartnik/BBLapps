"""
EPICSAreaDetectorCamera — camera backend for standard areaDetector IOCs.

Works against any IOC serving the genuine areaDetector record set: real
areaDetector (ADVimba, ADAravis, ...) or the VPCam contract IOCs
(vpcam/ioc/ad_ioc_base.py — Pi cameras, GigE cameras, CA-relay gateways).

PV naming convention (prefix = e.g. "VPCAM:01" or "VPCAMGW:02"):

    <prefix>:cam1:Acquire              "Acquire" / "Done"
    <prefix>:cam1:ImageMode            Single / Multiple / Continuous
    <prefix>:cam1:AcquireTime          set exposure (seconds)
    <prefix>:cam1:AcquireTime_RBV      readback
    <prefix>:cam1:AcquirePeriod        seconds/frame in Continuous mode
    <prefix>:cam1:Gain / Gain_RBV
    <prefix>:cam1:MinX / MinY / SizeX / SizeY (+_RBV)   ROI
    <prefix>:cam1:MaxSizeX_RBV / MaxSizeY_RBV           sensor size
    <prefix>:cam1:BitsPerPixel_RBV     true bit depth (VPCam extension,
                                       optional — falls back to DataType_RBV)
    <prefix>:image1:ArrayData          image waveform (uint16, row-major w*h)
    <prefix>:image1:ArraySize0_RBV     active frame width
    <prefix>:image1:ArraySize1_RBV     active frame height
    <prefix>:image1:ArrayCounter_RBV   monitor PV — fires when a frame is
                                       published (all metadata is already
                                       consistent when it fires)
"""

import os

os.environ.setdefault("EPICS_CA_MAX_ARRAY_BYTES", "40000000")

import numpy as np
import epics

from .base import CameraBase

# Bits carried by each areaDetector DataType, used when the IOC doesn't
# serve the BitsPerPixel_RBV extension
_DATATYPE_BITS = {
    "Int8": 8, "UInt8": 8,
    "Int16": 16, "UInt16": 16,
    "Int32": 32, "UInt32": 32,
    "Int64": 64, "UInt64": 64,
    "Float32": 32, "Float64": 64,
}


class EPICSAreaDetectorCamera(CameraBase):
    """
    Camera backend for standard areaDetector EPICS IOCs.

    epics_prefix: prefix WITHOUT trailing colon, e.g. "VPCAM:01"
    """

    def __init__(self, epics_prefix: str):
        self._prefix = epics_prefix.rstrip(":")

        # Monitor ArrayCounter_RBV — fires whenever a new frame is published
        self._new_frame = False
        self._monitor_pv = epics.PV(
            self._prefix + ":image1:ArrayCounter_RBV",
            callback=self._on_new_frame,
            auto_monitor=True,
        )

    # ------------------------------------------------------------------
    # CameraBase interface
    # ------------------------------------------------------------------

    @property
    def width(self) -> int:
        return int(self._get(":image1:ArraySize0_RBV") or 0)

    @property
    def height(self) -> int:
        return int(self._get(":image1:ArraySize1_RBV") or 0)

    @property
    def width_max(self) -> int:
        return int(self._get(":cam1:MaxSizeX_RBV") or self.width)

    @property
    def height_max(self) -> int:
        return int(self._get(":cam1:MaxSizeY_RBV") or self.height)

    @property
    def offset_x(self) -> int:
        return int(self._get(":cam1:MinX_RBV") or 0)

    @property
    def offset_y(self) -> int:
        return int(self._get(":cam1:MinY_RBV") or 0)

    @property
    def exposure_time(self) -> float:
        v = self._get(":cam1:AcquireTime_RBV")
        return float(v) if v is not None else 0.0

    @exposure_time.setter
    def exposure_time(self, value: float):
        epics.caput(self._prefix + ":cam1:AcquireTime", value)
        # Keep the frame period compatible with the exposure: at least 0.1 s
        # (<= 10 Hz) and long enough for a 95% duty cycle
        period = max(0.1, value / 0.95)
        epics.caput(self._prefix + ":cam1:AcquirePeriod", period)

    @property
    def gain(self) -> float:
        v = self._get(":cam1:Gain_RBV")
        return float(v) if v is not None else 1.0

    @gain.setter
    def gain(self, value: float):
        epics.caput(self._prefix + ":cam1:Gain", float(value))

    @property
    def bits(self) -> int:
        v = self._get(":cam1:BitsPerPixel_RBV")
        if v is not None:
            return int(v)
        # Genuine areaDetector has no true-bit-depth record; infer the
        # container size from DataType_RBV
        dt = epics.caget(self._prefix + ":cam1:DataType_RBV",
                         as_string=True, timeout=2.0)
        return _DATATYPE_BITS.get(dt, 16)

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
        # areaDetector stores row-major (h rows × w cols)
        return raw[: w * h].reshape(h, w)

    def has_new_frame(self) -> bool:
        if self._new_frame:
            self._new_frame = False
            return True
        return False

    def set_roi(self, x: int, y: int, w: int, h: int):
        """ROI records apply immediately; the IOC clamps out-of-range values.
        Offsets first so the sizes are clamped against the new origin."""
        epics.caput(self._prefix + ":cam1:MinX", int(x), wait=True)
        epics.caput(self._prefix + ":cam1:MinY", int(y), wait=True)
        epics.caput(self._prefix + ":cam1:SizeX", int(w), wait=True)
        epics.caput(self._prefix + ":cam1:SizeY", int(h), wait=True)

    def get_roi(self) -> tuple:
        return (
            int(self._get(":cam1:MinX_RBV") or 0),
            int(self._get(":cam1:MinY_RBV") or 0),
            int(self._get(":cam1:SizeX_RBV") or self.width),
            int(self._get(":cam1:SizeY_RBV") or self.height),
        )

    def start_streaming(self, rate_hz: float = 5.0):
        epics.caput(self._prefix + ":cam1:ImageMode", "Continuous")
        if rate_hz > 0:
            epics.caput(self._prefix + ":cam1:AcquirePeriod", 1.0 / rate_hz)
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
