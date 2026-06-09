import os
import numpy as np
import epics

from .base import CameraBase

# EPICS CA requires a larger-than-default buffer for image PVs.
os.environ.setdefault("EPICS_CA_MAX_ARRAY_BYTES", "40000000")


class VPCAMCamera(CameraBase):
    """
    Camera backend for the VPCam IOC (IMX296 or IMX708) via pyepics.

    epics_prefix: the full PV prefix, e.g. "VPCAM:01:GB"
    bits: 10 for IMX296, 8 for IMX708 (auto-read from IOC if possible)
    """

    def __init__(self, epics_prefix: str):
        # Normalise: strip trailing colon so we can always add one ourselves
        self._prefix = epics_prefix.rstrip(":")

        # Disable auto-trigger on init; the GUI timer drives frame capture.
        # We do this *before* reading anything so the IOC isn't flooding frames.
        self._put(":autotrigger_rate", 0)

        # Turn off auto-exposure so manual exposure/gain take effect
        self._put(":ae_enable", 0)

        # Read bits-per-pixel from the IOC (fallback to 8 if PV absent)
        bpp = epics.caget(self._prefix + ":bits_per_pixel", timeout=2.0)
        self._bits = int(bpp) if bpp is not None else 8

        # Subscribe to the frame timestamp so wait_for_new_frame works
        self._new_frame = False
        self._ts_pv = epics.PV(
            self._prefix + ":frame_timestamp",
            callback=self._on_timestamp,
            auto_monitor=True,
        )

    # ------------------------------------------------------------------
    # CameraBase interface
    # ------------------------------------------------------------------

    @property
    def width(self) -> int:
        return int(self._get(":frame_width_rdbk") or 0)

    @property
    def height(self) -> int:
        return int(self._get(":frame_height_rdbk") or 0)

    @property
    def width_max(self) -> int:
        v = self._get(":frame_width_max")
        if v is None:
            v = self._get(":sensor_width_max")
        return int(v) if v is not None else self.width

    @property
    def height_max(self) -> int:
        v = self._get(":frame_height_max")
        if v is None:
            v = self._get(":sensor_height_max")
        return int(v) if v is not None else self.height

    @property
    def offset_x(self) -> int:
        return int(self._get(":roi_x_rdbk") or 0)

    @property
    def offset_y(self) -> int:
        return int(self._get(":roi_y_rdbk") or 0)

    @property
    def exposure_time(self) -> float:
        us = self._get(":exposure_time_us")
        return float(us) / 1e6 if us is not None else 0.0

    @exposure_time.setter
    def exposure_time(self, value: float):
        # Set autotrigger rate to stay within 95% duty cycle
        max_rate = min(10.0, 0.95 / max(value, 1e-6))
        self._put(":autotrigger_rate", max_rate)
        self._put(":exposure_time_us", int(value * 1e6))

    @property
    def gain(self) -> float:
        v = self._get(":analogue_gain")
        return float(v) if v is not None else 1.0

    @gain.setter
    def gain(self, value: float):
        self._put(":analogue_gain", float(value))

    @property
    def bits(self) -> int:
        return self._bits

    def snapshot(self) -> np.ndarray:
        """Fetch the current frame from the IOC and return a 2-D uint16 array."""
        w = self.width
        h = self.height
        if w <= 0 or h <= 0:
            return np.zeros((1, 1), dtype=np.uint16)

        raw = epics.caget(
            self._prefix + ":frame_gray",
            count=w * h,
            timeout=5.0,
            as_numpy=True,
        )
        if raw is None:
            return np.zeros((h, w), dtype=np.uint16)

        raw = np.asarray(raw, dtype=np.uint16)
        if raw.size < w * h:
            # Pad if IOC returned fewer elements than expected
            padded = np.zeros(w * h, dtype=np.uint16)
            padded[: raw.size] = raw
            raw = padded
        return raw[: w * h].reshape(h, w)

    def set_roi(self, x: int, y: int, w: int, h: int):
        """Stage and apply a new ROI via the IOC's stage-then-apply PVs."""
        self._put(":roi_x_set",      x)
        self._put(":roi_y_set",      y)
        self._put(":roi_width_set",  w)
        self._put(":roi_height_set", h)
        # wait=True so the readback PVs are updated before we return
        epics.caput(self._prefix + ":roi_apply", 1, wait=True, timeout=10.0)

    def get_roi(self) -> tuple:
        """Return the active ROI as (x, y, w, h) from the IOC readbacks."""
        x = int(self._get(":roi_x_rdbk")      or 0)
        y = int(self._get(":roi_y_rdbk")      or 0)
        w = int(self._get(":roi_width_rdbk")  or self.width_max)
        h = int(self._get(":roi_height_rdbk") or self.height_max)
        return x, y, w, h

    def start_streaming(self, rate_hz: float = 5.0):
        """Ask the IOC to begin continuous frame capture at the given rate."""
        self._put(":autotrigger_rate", float(rate_hz))

    def stop_streaming(self):
        """Tell the IOC to stop continuous capture."""
        self._put(":autotrigger_rate", 0)

    def close(self):
        self._put(":autotrigger_rate", 0)
        self._ts_pv.disconnect()

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def _get(self, suffix: str, timeout: float = 2.0):
        return epics.caget(self._prefix + suffix, timeout=timeout)

    def _put(self, suffix: str, value, timeout: float = 2.0):
        epics.caput(self._prefix + suffix, value, timeout=timeout)

    def _on_timestamp(self, **kwargs):
        self._new_frame = True

    def has_new_frame(self) -> bool:
        """Non-blocking check: returns True and clears the flag if the CA monitor
        has fired since the last call (i.e. the IOC published a new frame)."""
        if self._new_frame:
            self._new_frame = False
            return True
        return False

    def wait_for_new_frame(self, timeout: float = 3.0) -> bool:
        """Block until the IOC publishes a new frame timestamp (or timeout)."""
        import time
        self._new_frame = False
        t0 = time.time()
        while not self._new_frame:
            if time.time() - t0 > timeout:
                return False
            time.sleep(0.005)
        return True
