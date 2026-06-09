"""
GigECamera — camera backend using the Harvester GenICam consumer.

Works with any GigE Vision camera (Allied Vision, FLIR Blackfly, etc.)
via the vendor's GenTL producer (.cti / .so file).

Requires:
    pip install harvesters

Vendor GenTL producers (install the native SDK, not pip):
    Allied Vision / Vimba:  e.g. /opt/Vimba_6_0/VimbaGigETL/lib/x86_64bit/libVimbaGigETL.so
    FLIR / Spinnaker:        e.g. /opt/spinnaker/lib/libSpinnaker_GenTL.so

YAML config:
    gentl_paths:
      - "/opt/Vimba_6_0/VimbaGigETL/lib/x86_64bit/libVimbaGigETL.so"
    cameras:
      - id: "192.168.128.2"
"""

import os
import numpy as np

from .base import CameraBase


class GigECamera(CameraBase):
    """
    GenICam-based GigE Vision camera backend via Harvester.

    Parameters
    ----------
    ip_address : str
        Camera IP address, e.g. "192.168.128.2"
    cti_paths : list[str]
        Paths to GenTL producer .cti / .so files.  At least one must exist.
    """

    def __init__(self, ip_address: str, cti_paths: list):
        from harvesters.core import Harvester

        self._h = Harvester()
        loaded = [p for p in cti_paths if os.path.exists(p)]
        if not loaded:
            raise RuntimeError(
                f"No GenTL producers found among: {cti_paths}\n"
                "Install the vendor SDK (Vimba, Spinnaker, …) and add the "
                ".cti / .so path to gentl_paths in your YAML config."
            )
        for p in loaded:
            self._h.add_file(p)
        self._h.update()

        try:
            self._ia = self._h.create({'DeviceIPAddress': ip_address})
        except Exception:
            # Fall back to first discovered camera if IP search fails
            self._ia = self._h.create(0)

        nm = self._ia.remote_device.node_map

        try:
            nm.AcquisitionMode.value = 'Continuous'
        except Exception:
            pass

        self._bits = self._init_pixel_format(nm)
        self._streaming = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _init_pixel_format(self, nm) -> int:
        """Set the cleanest (non-packed) pixel format and return bit depth."""
        for fmt, bits in [('Mono16', 16), ('Mono12', 12), ('Mono10', 10), ('Mono8', 8)]:
            try:
                nm.PixelFormat.value = fmt
                return bits
            except Exception:
                continue
        # Read whatever the camera already has
        try:
            pf = nm.PixelFormat.value
            for tag, b in [('16', 16), ('12', 12), ('10', 10)]:
                if tag in pf:
                    return b
        except Exception:
            pass
        return 8

    def _nm(self):
        return self._ia.remote_device.node_map

    def _get_node(self, *names):
        """Return the first node that exists, or None."""
        nm = self._nm()
        for name in names:
            try:
                return getattr(nm, name)
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # CameraBase interface
    # ------------------------------------------------------------------

    @property
    def width(self) -> int:
        return int(self._nm().Width.value)

    @property
    def height(self) -> int:
        return int(self._nm().Height.value)

    @property
    def width_max(self) -> int:
        node = self._get_node('WidthMax', 'SensorWidth')
        return int(node.value) if node else self.width

    @property
    def height_max(self) -> int:
        node = self._get_node('HeightMax', 'SensorHeight')
        return int(node.value) if node else self.height

    @property
    def offset_x(self) -> int:
        try:
            return int(self._nm().OffsetX.value)
        except Exception:
            return 0

    @property
    def offset_y(self) -> int:
        try:
            return int(self._nm().OffsetY.value)
        except Exception:
            return 0

    @property
    def exposure_time(self) -> float:
        node = self._get_node('ExposureTime', 'ExposureTimeAbs')
        return float(node.value) / 1e6 if node else 0.0  # µs → s

    @exposure_time.setter
    def exposure_time(self, value: float):
        node = self._get_node('ExposureTime', 'ExposureTimeAbs')
        if node is None:
            return
        us = value * 1e6
        try:
            us = max(node.min, min(node.max, us))
        except Exception:
            pass
        node.value = us

    @property
    def gain(self) -> float:
        node = self._get_node('Gain', 'GainRaw')
        return float(node.value) if node else 0.0

    @gain.setter
    def gain(self, value: float):
        node = self._get_node('Gain', 'GainRaw')
        if node:
            node.value = value

    @property
    def bits(self) -> int:
        return self._bits

    def snapshot(self) -> np.ndarray:
        was_stopped = not self._streaming
        if was_stopped:
            self._ia.start()
        try:
            with self._ia.fetch(timeout=5.0) as buffer:
                component = buffer.payload.components[0]
                data = component.data.copy()
                h = component.height
                w = component.width
        finally:
            if was_stopped:
                self._ia.stop()

        img = data.reshape(h, w)
        if img.dtype != np.uint16:
            img = img.astype(np.uint16)
        return img

    def set_roi(self, x: int, y: int, w: int, h: int):
        nm = self._nm()
        was_streaming = self._streaming
        if was_streaming:
            self._ia.stop()
        try:
            # Reset offsets first so width/height don't go out of range
            nm.OffsetX.value = 0
            nm.OffsetY.value = 0
            nm.Width.value  = w
            nm.Height.value = h
            nm.OffsetX.value = x
            nm.OffsetY.value = y
        except Exception as e:
            print(f"[gige roi] {e}")
        finally:
            if was_streaming:
                self._ia.start()

    def start_streaming(self, rate_hz: float = 5.0):
        if not self._streaming:
            try:
                nm = self._nm()
                nm.AcquisitionFrameRateEnable.value = True
                nm.AcquisitionFrameRate.value = float(rate_hz)
            except Exception:
                pass
            self._ia.start()
            self._streaming = True

    def stop_streaming(self):
        if self._streaming:
            self._ia.stop()
            self._streaming = False

    def close(self):
        self.stop_streaming()
        try:
            self._ia.destroy()
        except Exception:
            pass
        try:
            self._h.reset()
        except Exception:
            pass
