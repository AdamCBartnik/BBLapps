"""
mock_ioc.py — standalone mock camera IOC (no hardware).

Serves the standard-AD contract with a drifting Gaussian blob + noise,
mirroring beamview's MockCamera.  Useful for developing/testing any client
(beamview, web UI, Phoebus, the gateway) on a machine with no camera.

    python mock_ioc.py MOCKCAM:01
    python mock_ioc.py VPCAM:99 --rate 5

Usage:
    python mock_ioc.py PREFIX [--rate HZ] [caproto options, e.g. --list-pvs]
"""

from __future__ import annotations

import argparse
import sys
import time

import numpy as np

from ad_ioc_base import (AcquireState, CameraDriver, ExtensionPV, ImageMode,
                         build_ioc_class)


class MockDriver(CameraDriver):
    """Drifting Gaussian blob + noise, mirroring beamview's MockCamera."""

    manufacturer = "VPCam"
    model = "Mock Camera"
    SENSOR_W = 640
    SENSOR_H = 480

    extension_pvs = [
        ExtensionPV(name='Uptime_RBV', dtype=float, initial=0.0,
                    read_only=True, doc='IOC uptime (s)',
                    getter=lambda d: time.time() - d._t_start,
                    poll_period=5.0),
    ]

    def __init__(self, config: dict = None, **kw):
        self._roi = (0, 0, self.SENSOR_W, self.SENSOR_H)
        self._exp = 0.01
        self._gain = 1.0
        self._t_start = time.time()

    @property
    def sensor_width(self):
        return self.SENSOR_W

    @property
    def sensor_height(self):
        return self.SENSOR_H

    def get_roi(self):
        return self._roi

    def set_roi(self, x, y, w, h):
        x = max(0, min(int(x), self.SENSOR_W - 1))
        y = max(0, min(int(y), self.SENSOR_H - 1))
        w = max(1, min(int(w), self.SENSOR_W - x))
        h = max(1, min(int(h), self.SENSOR_H - y))
        self._roi = (x, y, w, h)
        return self._roi

    @property
    def exposure_time(self):
        return self._exp

    @exposure_time.setter
    def exposure_time(self, s):
        self._exp = max(1e-6, float(s))

    @property
    def gain(self):
        return self._gain

    @gain.setter
    def gain(self, v):
        self._gain = float(v)

    @property
    def bits_per_pixel(self):
        return 10

    def capture(self):
        t = time.time() - self._t_start
        x, y, w, h = self._roi
        yy, xx = np.mgrid[y:y + h, x:x + w]
        cx = self.SENSOR_W / 2 + 60 * np.sin(0.31 * t)
        cy = self.SENSOR_H / 2 + 45 * np.cos(0.23 * t)
        sig = 28 + 6 * np.sin(0.11 * t)
        amp = 700 * self._gain * (self._exp / 0.01)
        img = amp * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sig ** 2))
        img += np.random.normal(8, 4, img.shape)
        return np.clip(img, 0, 1023).astype(np.uint16)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args(argv):
    parser = argparse.ArgumentParser(
        add_help=False,
        description="Standalone mock camera IOC (no hardware)",
    )
    parser.add_argument("prefix", help="PV prefix to serve, e.g. MOCKCAM:01")
    parser.add_argument(
        "--rate", type=float, default=5.0,
        help="Start continuous acquisition at this rate on boot, Hz "
             "(0 = boot idle)",
    )
    args, remaining = parser.parse_known_args(argv[1:])
    sys.argv = [argv[0], *remaining]
    return args


def main():
    from caproto.server import ioc_arg_parser, run

    args = _parse_args(sys.argv)
    prefix = args.prefix.rstrip(":")

    driver = MockDriver()
    IOCClass = build_ioc_class(MockDriver)
    ioc_options, run_options = ioc_arg_parser(
        default_prefix=f"{prefix}:",
        desc=f"Mock camera IOC: {prefix}")
    ioc = IOCClass(driver=driver, **ioc_options)

    async def startup_hook(async_lib):
        await ioc.startup()
        if args.rate > 0:
            await ioc.cam1_AcquirePeriod.write(1.0 / args.rate)
            await ioc.cam1_ImageMode.write(ImageMode.Continuous)
            await ioc.cam1_Acquire.write(AcquireState.Acquire)
            print(f"[mock] continuous acquisition started at {args.rate} Hz")

    run(ioc.pvdb, startup_hook=startup_hook, **run_options)


if __name__ == "__main__":
    main()
