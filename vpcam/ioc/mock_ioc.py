"""
mock_ioc.py — standalone hardware-free mock camera IOC.

Serves the ad_ioc_base contract with no hardware and no config file, for
developing/testing any contract client (beamview, the web UI, a gateway)
anywhere — including Windows. It is deliberately NOT a vpcam_launcher camera
type: the mock has no hardware, no persisted settings, and nothing
VPCam-specific, so the config.yaml machinery would be pure overhead. Like
gateway_ioc.py / aravis_ioc.py, it is a standalone, arg-driven tool.

It is a dual-frame ("double") camera: each acquisition publishes TWO raw
Gaussian frames, image1 = "cold" and image2 = "hot" (~10% dimmer), so a
client can form Normal = image1+image2, Cold = image1, Hot = image2,
Diff = image2-image1. The IOC does no math — the client combines.

Usage:
    python mock_ioc.py                 -> serves MOCK:cam1:..., MOCK:image1:...,
                                          MOCK:image2:...  streaming at 5 Hz
    python mock_ioc.py --prefix SIM:01 --rate 10
    python mock_ioc.py --rate 0        -> boot idle (client drives cam1:Acquire)
    python mock_ioc.py --list-pvs      -> print the served PV table and exit
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# Frames are multi-MB waveforms
os.environ.setdefault("EPICS_CA_MAX_ARRAY_BYTES", "40000000")

import numpy as np

from ad_ioc_base import (AcquireState, CameraDriver, ExtensionPV, ImageMode,
                         build_ioc_class)


def _clamp_roi(x, y, w, h, max_w, max_h):
    x = max(0, min(int(x), max_w - 1))
    y = max(0, min(int(y), max_h - 1))
    w = max(1, min(int(w), max_w - x))
    h = max(1, min(int(h), max_h - y))
    return x, y, w, h


class MockDriver(CameraDriver):
    """Dual-frame ("double") mock: publishes two raw Gaussian frames per
    acquisition, image1 = "cold" and image2 = "hot" (~10% dimmer). No
    hardware; runs anywhere."""

    manufacturer = "VPCam"
    model = "Mock Double Camera"
    SENSOR_W = 1000
    SENSOR_H = 1000
    dual_frame = True

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
        self._roi = _clamp_roi(x, y, w, h, self.SENSOR_W, self.SENSOR_H)
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
        return 12

    def capture(self):
        """Return (cold, hot): two raw uint16 frames from the same shot.

        The beam lives in fixed sensor coordinates (drifts about the sensor
        centre) so a hardware ROI shows the matching crop. "hot" carries 10%
        less beam magnitude than "cold"; both are scaled so their sum peaks
        near full scale."""
        time.sleep(self._exp)
        t = time.time() - self._t_start
        x, y, w, h = self._roi
        yy, xx = np.mgrid[y:y + h, x:x + w]

        cx = self.SENSOR_W / 2 + 30 * np.sin(0.3 * t)
        cy = self.SENSOR_H / 2 + 20 * np.sin(0.2 * t + 1.0)
        sx = self.SENSOR_W * 0.08
        sy = self.SENSOR_H * 0.06
        beam = np.exp(-0.5 * ((xx - cx) / sx) ** 2 - 0.5 * ((yy - cy) / sy) ** 2)

        max_value = 2 ** self.bits_per_pixel - 1
        peak = max_value * 0.42 * self._gain * (self._exp / 0.01)
        nstd = max_value * 0.002

        def frame(amp):
            out = beam * amp + np.random.normal(0, nstd, beam.shape)
            return np.clip(out, 0, max_value).astype(np.uint16)

        cold = frame(peak)
        hot = frame(0.9 * peak)
        return cold, hot


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args(argv):
    parser = argparse.ArgumentParser(
        add_help=False,
        description="Standalone hardware-free dual-frame mock camera IOC",
    )
    parser.add_argument(
        "--prefix", default="MOCK",
        help="PV prefix to serve (default: MOCK -> MOCK:cam1:..., "
             "MOCK:image1:..., MOCK:image2:...)",
    )
    parser.add_argument(
        "--rate", type=float, default=5.0,
        help="Continuous acquisition rate in Hz on boot (default 5.0; "
             "0 = boot idle, client drives cam1:Acquire)",
    )
    args, remaining = parser.parse_known_args(argv[1:])
    # Hand any leftover options (e.g. --list-pvs) to caproto's own parser
    sys.argv = [argv[0], *remaining]
    return args


def main():
    from caproto.server import ioc_arg_parser, run

    args = _parse_args(sys.argv)
    prefix = args.prefix.rstrip(":")

    print(f"[mock] dual-frame mock camera -> {prefix}: "
          f"({MockDriver.SENSOR_W}x{MockDriver.SENSOR_H}, "
          f"{'streaming %.3g Hz' % args.rate if args.rate > 0 else 'idle'})")

    driver = MockDriver()

    IOCClass = build_ioc_class(MockDriver)
    ioc_options, run_options = ioc_arg_parser(
        default_prefix=f"{prefix}:",
        desc=f"Mock dual-frame camera IOC ({prefix})")
    ioc = IOCClass(driver=driver, **ioc_options)

    async def startup_hook(async_lib):
        await ioc.startup()
        if args.rate > 0:
            await ioc.cam1_AcquirePeriod.write(1.0 / args.rate)
            await ioc.cam1_ImageMode.write(ImageMode.Continuous)
            await ioc.cam1_Acquire.write(AcquireState.Acquire)
            print(f"[mock] continuous acquisition started at {args.rate} Hz")

    run(ioc.pvdb, startup_hook=startup_hook, **run_options)


if __name__ == '__main__':
    main()
