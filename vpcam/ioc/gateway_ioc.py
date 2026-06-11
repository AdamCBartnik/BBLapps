"""
gateway_ioc.py — standalone CA gateway for any standard-areaDetector IOC.

Relays a source IOC serving the ad_ioc_base contract (a VPCam Pi camera, a
GigE-camera IOC, or anything else that looks like an areaDetector) to a new
prefix: the source prefix with ":GW" appended.  Because source and gateway
share the same PV contract, the relay is name-preserving — no translation.

    python gateway_ioc.py VPCAM:03
        -> serves VPCAM:03:GW:cam1:..., VPCAM:03:GW:image1:...

Why a gateway: the source IOC (e.g. a Pi on a constrained link) serves ONE
image consumer — this gateway — which fans frames out to any number of CA
clients on the wider network.  Image traffic from the source happens only
while the gateway is acquiring, via counted reads sized to the active ROI.

The relay is event-driven: it camonitors the source's image1:ArrayCounter_RBV
and fetches/republishes each frame as it appears — no pacing of its own, and
it can never outpace (or re-publish stale frames from) the source.  If the
network can't keep up with the source rate, intermediate frames are simply
skipped; the newest one always wins.

Extension PVs (LED, autofocus, system info, ...) are mirrored according to
--extensions; the default sniffs the prefix: a VPCam source gets the common
VPCam set, anything else gets the standard records only.

The gateway boots relaying.  cam1:Acquire gates the relay traffic: starting
also starts the source (idempotent); stopping the gateway does NOT stop the
source, so e.g. the Pi's local web UI keeps streaming.

Usage:
    python gateway_ioc.py SOURCE_PREFIX [--extensions auto|none|common|imx708]
                          [caproto options, e.g. --list-pvs]
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

# Counted reads from the source can be multi-MB image frames
os.environ.setdefault("EPICS_CA_MAX_ARRAY_BYTES", "40000000")

import numpy as np

from ad_ioc_base import (AcquireState, CameraDriver, ExtensionPV, ImageMode,
                         build_ioc_class)

# The relay is event-driven: capture() waits on the source's
# image1:ArrayCounter_RBV monitor and returns None on timeout (the loop just
# retries).  The timeout exists only so an Acquire-stop can wind the capture
# thread down promptly.
EVENT_WAIT = 1.0
CA_TIMEOUT = 3.0
# If the gateway is acquiring but the source produces no frames for this many
# consecutive event timeouts (~seconds), check the source and re-assert its
# acquisition.  Covers: source stopped by another client, source/Pi rebooted
# under a running gateway, gateway already-acquiring when a client "starts" it.
STARVE_RETRIES = 3


def _scalarize(data):
    """caproto threading-client response data -> python scalar/str."""
    if isinstance(data, np.ndarray):
        if data.size == 0:
            return ""
        if data.dtype.kind in "SUO":
            v = data[0]
            return v.decode(errors="replace") if isinstance(v, bytes) else str(v)
        if data.size == 1:
            return data.item()
        if data.dtype.kind in "iu" and data.size <= 256:
            try:
                return bytes(int(x) for x in data).decode(
                    "ascii", errors="replace").rstrip("\x00").strip()
            except Exception:
                pass
        return data
    if isinstance(data, (list, tuple)) and len(data) == 1:
        v = data[0]
        return v.decode(errors="replace") if isinstance(v, bytes) else v
    return data


class CARelayDriver(CameraDriver):
    """CameraDriver whose "camera" is another contract-serving IOC."""

    manufacturer = "VPCam"
    model = "CA gateway"        # replaced with source model at construction
    extension_pvs: list = []    # set by make_relay_driver_class()

    def __init__(self, source_prefix: str):
        from caproto.threading.client import Context

        self.source_prefix = source_prefix.rstrip(":")
        self._ctx = Context()
        self._pvs: dict = {}
        self._subs: list = []
        self._new_frame = threading.Event()
        self._starve_count = 0

        # Static identity/geometry from the source
        self._w_max = int(self.read_scalar("cam1:MaxSizeX_RBV"))
        self._h_max = int(self.read_scalar("cam1:MaxSizeY_RBV"))
        self._bits = int(self.read_scalar("cam1:BitsPerPixel_RBV"))
        try:
            src_model = str(self.read_scalar("cam1:Model_RBV"))
            type(self).model = f"{src_model} (via gateway)"
        except Exception:
            pass

        # New-frame monitor — drives capture() pacing.
        # caproto's threading client holds callbacks by WEAK reference: a
        # lambda would be garbage-collected immediately and the monitor would
        # silently never fire (the old vpcam_gateway_ioc.py worked around
        # exactly this with a polling fallback).  Keep a strong reference.
        self._counter_callback = self._on_counter_event
        sub = self._pv("image1:ArrayCounter_RBV").subscribe()
        sub.add_callback(self._counter_callback)
        self._subs.append(sub)

        print(f"[gateway] source {self.source_prefix}: "
              f"{self._w_max}x{self._h_max}, {self._bits}-bit")

    def _on_counter_event(self, sub, response):
        self._new_frame.set()

    # -- CA helpers -----------------------------------------------------------

    def _pv(self, suffix: str):
        if suffix not in self._pvs:
            (pv,) = self._ctx.get_pvs(f"{self.source_prefix}:{suffix}",
                                      timeout=CA_TIMEOUT)
            self._pvs[suffix] = pv
        return self._pvs[suffix]

    def read_scalar(self, suffix: str):
        return _scalarize(self._pv(suffix).read(timeout=CA_TIMEOUT).data)

    def write_scalar(self, suffix: str, value):
        self._pv(suffix).write(value, wait=True, timeout=CA_TIMEOUT)
        return value

    # -- geometry --------------------------------------------------------------

    @property
    def sensor_width(self) -> int:
        try:
            self._w_max = int(self.read_scalar("cam1:MaxSizeX_RBV"))
        except Exception:
            pass
        return self._w_max

    @property
    def sensor_height(self) -> int:
        try:
            self._h_max = int(self.read_scalar("cam1:MaxSizeY_RBV"))
        except Exception:
            pass
        return self._h_max

    def get_roi(self):
        return (int(self.read_scalar("cam1:MinX_RBV")),
                int(self.read_scalar("cam1:MinY_RBV")),
                int(self.read_scalar("cam1:SizeX_RBV")),
                int(self.read_scalar("cam1:SizeY_RBV")))

    def set_roi(self, x, y, w, h):
        self.write_scalar("cam1:MinX", int(x))
        self.write_scalar("cam1:MinY", int(y))
        self.write_scalar("cam1:SizeX", int(w))
        self.write_scalar("cam1:SizeY", int(h))
        return self.get_roi()

    # -- exposure / gain ---------------------------------------------------------

    @property
    def exposure_time(self) -> float:
        return float(self.read_scalar("cam1:AcquireTime_RBV"))

    @exposure_time.setter
    def exposure_time(self, seconds: float):
        self.write_scalar("cam1:AcquireTime", float(seconds))

    @property
    def gain(self) -> float:
        return float(self.read_scalar("cam1:Gain_RBV"))

    @gain.setter
    def gain(self, value: float):
        self.write_scalar("cam1:Gain", float(value))

    @property
    def bits_per_pixel(self) -> int:
        return self._bits

    # -- calibration (base serves CalibX/Y from these) -----------------------------

    def load_calibration(self):
        try:
            return (float(self.read_scalar("cam1:CalibX")),
                    float(self.read_scalar("cam1:CalibY")))
        except Exception:
            return None

    def save_calibration(self, cal_x_um, cal_y_um):
        try:
            self.write_scalar("cam1:CalibX", float(cal_x_um))
            self.write_scalar("cam1:CalibY", float(cal_y_um))
        except Exception as e:
            print(f"[gateway] calibration write failed: {e}")

    # -- acquisition ----------------------------------------------------------------

    def on_acquire_start(self):
        # Ensure the source is streaming (idempotent).  Deliberately never
        # stopped on on_acquire_stop: other consumers may be watching.
        try:
            self.write_scalar("cam1:ImageMode", 2)   # Continuous
            self.write_scalar("cam1:Acquire", 1)
        except Exception as e:
            print(f"[gateway] could not start source acquisition: {e}")
        self._new_frame.clear()

    def capture(self):
        # Event-driven: relay a frame only when the source's counter monitor
        # says a new one was published.  No event -> None -> the loop retries
        # (never re-publishes stale data, never outpaces the source).
        if not self._new_frame.wait(timeout=EVENT_WAIT):
            # Starving: we're acquiring but the source isn't producing.
            # After a few quiet seconds, re-assert source acquisition.
            self._starve_count += 1
            if self._starve_count >= STARVE_RETRIES:
                self._starve_count = 0
                try:
                    acq = self.read_scalar("cam1:Acquire_RBV")
                    if acq in (0, "Done"):
                        print("[gateway] source idle while relaying — "
                              "restarting source acquisition")
                        self.write_scalar("cam1:ImageMode", 2)
                        self.write_scalar("cam1:Acquire", 1)
                except Exception as e:
                    print(f"[gateway] source check failed (will retry): {e}")
            return None
        self._new_frame.clear()
        self._starve_count = 0

        # All failures here are "no frame this time", not fatal: a freshly
        # booted source reports ArraySize0/1 = 0 until its first frame, and
        # CA reads can time out transiently (source restart, network blip).
        # The relay must survive all of that and keep listening.
        try:
            w = int(self.read_scalar("image1:ArraySize0_RBV"))
            h = int(self.read_scalar("image1:ArraySize1_RBV"))
            if w <= 0 or h <= 0:
                return None     # source hasn't published a frame yet
            n = w * h
            data = self._pv("image1:ArrayData").read(
                data_count=n, timeout=CA_TIMEOUT + 5.0).data
            arr = np.asarray(data, dtype=np.uint16).reshape(-1)
            if arr.size < n:
                arr = np.pad(arr, (0, n - arr.size))
            return arr[:n].reshape(h, w)
        except Exception as e:
            print(f"[gateway] frame fetch failed (will retry): {e}")
            return None

    def close(self):
        try:
            self._ctx.disconnect()
        except Exception:
            pass


def _relay_get(suffix):
    def _get(d):
        return d.read_scalar(suffix)
    return _get


def _relay_set(suffix):
    def _set(d, v):
        d.write_scalar(suffix, v)
        return v
    return _set


def make_relay_driver_class(extension_set: str = "none"):
    """Build a CARelayDriver subclass mirroring the chosen extension PVs.

    The extension *specs* (names, types, docs) are reused from the Pi driver
    definitions; only the accessors are rebound to CA relay calls.
    """
    from vpcam_drivers import COMMON_EXTENSION_PVS, IMX708Driver

    if extension_set == "none":
        specs = []
    elif extension_set == "imx708":
        specs = list(IMX708Driver.extension_pvs)
    else:
        specs = list(COMMON_EXTENSION_PVS)

    exts = []
    for spec in specs:
        suffix = f"cam1:{spec.name}"
        # Source RBVs update via their own pollers; poll them here as well so
        # the gateway copy stays fresh without a per-PV CA monitor.
        poll = spec.poll_period if spec.poll_period > 0 else (
            5.0 if spec.name.endswith("_RBV") else 0.0)
        exts.append(ExtensionPV(
            name=spec.name,
            dtype=spec.dtype,
            initial=spec.initial,
            doc=f"RELAY: {spec.doc}",
            read_only=spec.read_only,
            getter=_relay_get(suffix),
            setter=None if spec.read_only else _relay_set(suffix),
            poll_period=poll,
            max_length=spec.max_length,
        ))

    return type(f"CARelayDriver_{extension_set}", (CARelayDriver,),
                {"extension_pvs": exts})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args(argv):
    parser = argparse.ArgumentParser(
        add_help=False,
        description="Standalone CA gateway for a standard-areaDetector IOC",
    )
    parser.add_argument(
        "source_prefix",
        help="PV prefix of the source IOC, e.g. VPCAM:03 "
             "(gateway serves <prefix>:GW)",
    )
    parser.add_argument(
        "--extensions", choices=("auto", "none", "common", "imx708"),
        default="auto",
        help="Which extension PVs to mirror. auto (default): the common "
             "VPCam set if 'VPCAM' is in the prefix, otherwise none.",
    )
    args, remaining = parser.parse_known_args(argv[1:])
    # Hand any leftover options (e.g. --list-pvs) to caproto's own parser
    sys.argv = [argv[0], *remaining]
    return args


def main():
    from caproto.server import ioc_arg_parser, run

    args = _parse_args(sys.argv)
    source = args.source_prefix.rstrip(":")
    public = f"{source}:GW"

    extension_set = args.extensions
    if extension_set == "auto":
        extension_set = "common" if "VPCAM" in source.upper() else "none"

    print(f"[gateway] {source}  ->  {public}:  (extensions: {extension_set})")

    DriverCls = make_relay_driver_class(extension_set)
    driver = DriverCls(source)

    IOCClass = build_ioc_class(DriverCls)
    ioc_options, run_options = ioc_arg_parser(
        default_prefix=f"{public}:",
        desc=f"CA gateway: {public} -> {source}")
    ioc = IOCClass(driver=driver, **ioc_options)

    async def startup_hook(async_lib):
        await ioc.startup()
        # Event-driven: no pacing of our own — every source frame is relayed
        # as its ArrayCounter monitor fires.  cam1:Acquire still gates the
        # relay traffic on/off.
        await ioc.cam1_AcquirePeriod.write(0.0)
        await ioc.cam1_ImageMode.write(ImageMode.Continuous)
        await ioc.cam1_Acquire.write(AcquireState.Acquire)
        print("[gateway] relaying every source frame (event-driven)")

    run(ioc.pvdb, startup_hook=startup_hook, **run_options)


if __name__ == "__main__":
    main()
