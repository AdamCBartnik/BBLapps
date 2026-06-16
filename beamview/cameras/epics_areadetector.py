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
import time

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

    def __init__(self, epics_prefix: str, dual_frame: bool = False):
        self._prefix = epics_prefix.rstrip(":")

        # Whether this is a "double" detector that serves a parallel image2:*
        # surface (Hot/Cold/Diff). Declared by the caller (config/CLI) rather
        # than probed: a runtime probe would stall startup ~2 s per
        # single-frame camera waiting for an image2 PV that never connects.
        self._has_image2 = bool(dual_frame)

        # Cache of persistent, auto-monitored PVs. Reads are served from the
        # local monitor cache (get(use_monitor=True)) — no per-frame CA
        # round-trips. A fresh caget costs ~15-20 ms each in pyepics; a frame
        # needs several scalars + the big ArrayData(s), which used to exceed
        # the inter-frame interval and trigger the atomicity retry loop.
        self._pvs: dict = {}

        # Monitor ArrayCounter_RBV — fires whenever a new frame is published.
        # The IOC writes it LAST, after both images and their UniqueIds, so
        # once this has fired the cached image/uid PVs are a consistent frame.
        self._new_frame = False
        self._monitor_pv = epics.PV(
            self._prefix + ":image1:ArrayCounter_RBV",
            callback=self._on_new_frame,
            auto_monitor=True,
        )

    def _pv(self, suffix: str) -> "epics.PV":
        """Return a cached, auto-monitored PV for prefix+suffix."""
        pv = self._pvs.get(suffix)
        if pv is None:
            pv = epics.PV(self._prefix + suffix, auto_monitor=True)
            self._pvs[suffix] = pv
        return pv

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

    def _read_image(self, name: str) -> np.ndarray:
        """Read one image waveform (name = 'image1' or 'image2'), reshaped to
        (h, w). The native EPICS dtype is preserved — a real two-image
        detector serves Float64 here, the mock serves UInt16 — so the
        downstream float pipeline never has to assume uint16."""
        w = int(self._get(f":{name}:ArraySize0_RBV") or 0)
        h = int(self._get(f":{name}:ArraySize1_RBV") or 0)
        if w <= 0 or h <= 0:
            return np.zeros((1, 1), dtype=np.uint16)

        raw = self._pv(f":{name}:ArrayData").get(
            count=w * h, timeout=5.0, as_numpy=True, use_monitor=True)
        if raw is None:
            return np.zeros((h, w), dtype=np.uint16)

        raw = np.asarray(raw)
        if raw.size < w * h:
            padded = np.zeros(w * h, dtype=raw.dtype)
            padded[: raw.size] = raw
            raw = padded
        # areaDetector stores row-major (h rows × w cols)
        return raw[: w * h].reshape(h, w)

    def snapshot(self) -> np.ndarray:
        return self._read_image("image1")

    @property
    def has_dual_frame(self) -> bool:
        return self._has_image2

    def snapshot_dual(self):
        """Return (image1, image2) from the same acquisition. For single-frame
        cameras image2 is None.

        Atomicity: reads come from the auto-monitor cache. The IOC sends a
        frame's fields in order on one CA circuit (image1, then image2, then
        the image1 counter that gates has_new_frame), so a settled cache holds
        a matched pair. The race is the NEXT frame arriving mid-read —
        image1's cache can update to N+1 while image2's monitor for N+1 hasn't
        been processed yet. We detect that by requiring image1 and image2
        UniqueIds to agree; on a mismatch we briefly yield so the lagging
        monitor callback lands, then re-read (the cached re-read is ~free)."""
        if not self._has_image2:
            return self.snapshot(), None

        img1 = img2 = None
        uid1 = uid2 = None
        for attempt in range(8):
            if attempt:
                time.sleep(0.002)   # let the in-flight monitor batch settle
            uid1 = self._get(":image1:UniqueId_RBV")
            uid2 = self._get(":image2:UniqueId_RBV")
            img1 = self._read_image("image1")
            img2 = self._read_image("image2")
            if uid1 is not None and uid1 == uid2:
                return img1, img2
        print(f"[epics] dual-frame UniqueId mismatch "
              f"(image1={uid1}, image2={uid2}); using latest read")
        return img1, img2

    def has_new_frame(self) -> bool:
        if self._new_frame:
            self._new_frame = False
            return True
        return False

    def set_roi(self, x: int, y: int, w: int, h: int):
        """Set the hardware ROI, ordered to avoid the areaDetector offset-clamp
        trap: setting MinX while the old SizeX is still large makes the camera
        clamp the offset (often to 0). So zero the offsets first, then set the
        sizes, then the offsets — every intermediate state stays in range."""
        p = self._prefix
        epics.caput(p + ":cam1:MinX", 0, wait=True)
        epics.caput(p + ":cam1:MinY", 0, wait=True)
        epics.caput(p + ":cam1:SizeX", int(w), wait=True)
        epics.caput(p + ":cam1:SizeY", int(h), wait=True)
        epics.caput(p + ":cam1:MinX", int(x), wait=True)
        epics.caput(p + ":cam1:MinY", int(y), wait=True)

    def get_roi(self) -> tuple:
        return (
            int(self._get(":cam1:MinX_RBV") or 0),
            int(self._get(":cam1:MinY_RBV") or 0),
            int(self._get(":cam1:SizeX_RBV") or self.width),
            int(self._get(":cam1:SizeY_RBV") or self.height),
        )

    def start_streaming(self, rate_hz: float = 10.0):
        # 10 Hz matches the exposure-setter's period floor (max(0.1, exp/0.95)),
        # so a fresh start runs at the same max rate a short-exposure write
        # produces — no more starting slow at 5 Hz until exposure is touched.
        epics.caput(self._prefix + ":cam1:ImageMode", "Continuous")
        if rate_hz > 0:
            epics.caput(self._prefix + ":cam1:AcquirePeriod", 1.0 / rate_hz)
        epics.caput(self._prefix + ":cam1:Acquire", "Acquire")

    def stop_streaming(self):
        epics.caput(self._prefix + ":cam1:Acquire", "Done")

    def close(self):
        self.stop_streaming()
        self._monitor_pv.disconnect()
        for pv in self._pvs.values():
            try:
                pv.disconnect()
            except Exception:
                pass
        self._pvs.clear()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get(self, suffix: str, timeout: float = 2.0):
        # Served from the auto-monitor cache after the first update — avoids a
        # ~15-20 ms CA round-trip on every per-frame scalar read.
        return self._pv(suffix).get(timeout=timeout, use_monitor=True)

    def _on_new_frame(self, **kwargs):
        self._new_frame = True
