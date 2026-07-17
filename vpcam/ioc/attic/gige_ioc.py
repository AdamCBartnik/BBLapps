"""
RETIRED (2026-07-16) — superseded by aravis_ioc.py for all deployments.

This is the vendor-GenTL (harvesters) implementation; it needs a vendor
SDK's .cti producer, which the old lab machines can't run. aravis_ioc.py
does the same job vendor-free and is what actually runs everywhere. Kept
only in case a camera ever needs a vendor-SDK-only GenICam feature.
Note it predates later contract additions (e.g. calibration persistence,
--swap-endian) — bring it up to date before reviving it.

gige_ioc.py — standalone IOC for GigE Vision cameras via Harvester.

Runs on a machine that shares a subnet with the camera (GigE Vision
requirement) and serves the standard-AD contract to the rest of the network.
No separate gateway is needed: GigE Vision allows one control connection per
camera, and this IOC *is* that connection — CA does the fan-out.

    python gige_ioc.py 192.168.128.2 B29CAM1
        -> serves B29CAM1:cam1:..., B29CAM1:image1:...

Ported from beamview/cameras/gige.py (hardware-validated logic).

Requires:
    pip install harvesters
plus a vendor GenTL producer (.cti / .so) — install the native SDK:
    Allied Vision Vimba X:  .../VimbaX_2026-1/cti/VimbaGigETL.cti
    FLIR Spinnaker:         .../lib/libSpinnaker_GenTL.so

Usage:
    python gige_ioc.py CAMERA_IP PREFIX [--gentl-path FILE.cti]
                       [--rate HZ] [caproto options, e.g. --list-pvs]
"""

from __future__ import annotations

import argparse
import os
import sys
import threading

import numpy as np

from ad_ioc_base import AcquireState, CameraDriver, ImageMode, build_ioc_class

if sys.platform.startswith("win"):
    DEFAULT_GENTL_PATHS = [
        "C:/Program Files/Allied Vision/VimbaX_2026-1/cti/VimbaGigETL.cti",
    ]
else:
    DEFAULT_GENTL_PATHS = [
        "/nfs/acc/temp/bblopr/cameras/VimbaX_2026-1/cti/VimbaGigETL.cti",
    ]


class GigEDriver(CameraDriver):
    manufacturer = "GigE Vision"
    extension_pvs: list = []

    def __init__(self, ip_address: str, gentl_paths: list | None = None):
        from harvesters.core import Harvester

        cti_paths = list(gentl_paths or DEFAULT_GENTL_PATHS)

        self._h = Harvester()
        loaded = [p for p in cti_paths if os.path.exists(p)]
        if not loaded:
            raise RuntimeError(
                f"No GenTL producers found among: {cti_paths}\n"
                "Install the vendor SDK (Vimba X, Spinnaker, ...) and pass "
                "--gentl-path."
            )
        for p in loaded:
            self._h.add_file(p)
        self._h.update()

        devices = self._h.device_info_list
        if not devices:
            raise RuntimeError(
                f"GenTL producer loaded ({loaded[0]}) but discovered 0 "
                "devices. GigE Vision discovery is a subnet broadcast: this "
                "machine needs a network interface ON the camera's subnet "
                "(routing to it is not enough — check `ip -4 addr`). Also "
                "check the camera is powered, and that no other application "
                "holds its control channel."
            )
        print(f"[gige] discovered {len(devices)} device(s):")
        for d in devices:
            print(f"[gige]   {d}")

        try:
            self._ia = self._h.create({"DeviceIPAddress": ip_address})
        except Exception:
            # Fall back to first discovered camera if IP search fails
            print(f"[gige] no device matched IP {ip_address!r}; "
                  "using the first discovered device")
            self._ia = self._h.create(0)

        nm = self._nm()
        try:
            type(self).manufacturer = str(nm.DeviceVendorName.value)
            type(self).model = str(nm.DeviceModelName.value)
        except Exception:
            type(self).model = f"GigE camera @ {ip_address}"

        try:
            nm.AcquisitionMode.value = "Continuous"
        except Exception:
            pass

        self._bits = self._init_pixel_format(nm)
        self._streaming = False
        # harvesters image-acquirer calls are not thread-safe across the
        # acquisition loop and putters running on different worker threads
        self._lock = threading.Lock()

    # -- helpers ---------------------------------------------------------------

    def _nm(self):
        return self._ia.remote_device.node_map

    def _get_node(self, *names):
        nm = self._nm()
        for name in names:
            try:
                return getattr(nm, name)
            except Exception:
                continue
        return None

    def _init_pixel_format(self, nm) -> int:
        """Set the cleanest (non-packed) pixel format and return bit depth."""
        for fmt, bits in [("Mono16", 16), ("Mono12", 12),
                          ("Mono10", 10), ("Mono8", 8)]:
            try:
                nm.PixelFormat.value = fmt
                return bits
            except Exception:
                continue
        try:
            pf = nm.PixelFormat.value
            for tag, b in [("16", 16), ("12", 12), ("10", 10)]:
                if tag in pf:
                    return b
        except Exception:
            pass
        return 8

    # -- geometry ----------------------------------------------------------------

    @property
    def sensor_width(self) -> int:
        node = self._get_node("WidthMax", "SensorWidth")
        return int(node.value) if node else int(self._nm().Width.value)

    @property
    def sensor_height(self) -> int:
        node = self._get_node("HeightMax", "SensorHeight")
        return int(node.value) if node else int(self._nm().Height.value)

    def get_roi(self):
        nm = self._nm()
        try:
            return (int(nm.OffsetX.value), int(nm.OffsetY.value),
                    int(nm.Width.value), int(nm.Height.value))
        except Exception:
            return (0, 0, self.sensor_width, self.sensor_height)

    def set_roi(self, x, y, w, h):
        nm = self._nm()
        with self._lock:
            was_streaming = self._streaming
            if was_streaming:
                self._ia.stop()
            try:
                # Reset offsets first so width/height don't go out of range
                nm.OffsetX.value = 0
                nm.OffsetY.value = 0
                nm.Width.value = int(w)
                nm.Height.value = int(h)
                nm.OffsetX.value = int(x)
                nm.OffsetY.value = int(y)
            except Exception as e:
                print(f"[gige] roi: {e}")
            finally:
                if was_streaming:
                    self._ia.start()
        return self.get_roi()

    # -- exposure / gain ------------------------------------------------------------

    @property
    def exposure_time(self) -> float:
        node = self._get_node("ExposureTime", "ExposureTimeAbs")
        return float(node.value) / 1e6 if node else 0.0  # us -> s

    @exposure_time.setter
    def exposure_time(self, seconds: float):
        node = self._get_node("ExposureTime", "ExposureTimeAbs")
        if node is None:
            return
        us = seconds * 1e6
        try:
            us = max(node.min, min(node.max, us))
        except Exception:
            pass
        with self._lock:
            node.value = us

    @property
    def gain(self) -> float:
        node = self._get_node("Gain", "GainRaw")
        return float(node.value) if node else 0.0

    @gain.setter
    def gain(self, value: float):
        node = self._get_node("Gain", "GainRaw")
        if node:
            with self._lock:
                node.value = value

    @property
    def bits_per_pixel(self) -> int:
        return self._bits

    # -- acquisition -------------------------------------------------------------------

    def on_acquire_start(self):
        with self._lock:
            if not self._streaming:
                self._ia.start()
                self._streaming = True

    def on_acquire_stop(self):
        with self._lock:
            if self._streaming:
                self._ia.stop()
                self._streaming = False

    def capture(self) -> np.ndarray:
        with self._lock:
            started_here = not self._streaming
            if started_here:
                self._ia.start()
            try:
                with self._ia.fetch(timeout=5.0) as buffer:
                    component = buffer.payload.components[0]
                    data = component.data.copy()
                    h = int(component.height)
                    w = int(component.width)
            finally:
                if started_here:
                    self._ia.stop()

        img = data.reshape(h, w)
        if img.dtype != np.uint16:
            img = img.astype(np.uint16)
        return img

    def close(self):
        try:
            self.on_acquire_stop()
        except Exception:
            pass
        try:
            self._ia.destroy()
        except Exception:
            pass
        try:
            self._h.reset()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args(argv):
    parser = argparse.ArgumentParser(
        add_help=False,
        description="Standalone IOC for a GigE Vision camera",
    )
    parser.add_argument(
        "camera_ip",
        help="Camera IP address, e.g. 192.168.128.2 "
             "(falls back to first discovered camera)",
    )
    parser.add_argument(
        "prefix",
        help="PV prefix to serve, e.g. B29CAM1",
    )
    parser.add_argument(
        "--gentl-path", action="append", default=None, metavar="FILE",
        help="GenTL producer (.cti / .so); repeatable. "
             "Default: the VimbaX producer for this OS.",
    )
    parser.add_argument(
        "--rate", type=float, default=0.0,
        help="Start continuous acquisition at this rate on boot, Hz "
             "(default 0 = boot idle; clients start via cam1:Acquire)",
    )
    args, remaining = parser.parse_known_args(argv[1:])
    # Hand any leftover options (e.g. --list-pvs) to caproto's own parser
    sys.argv = [argv[0], *remaining]
    return args


def main():
    from caproto.server import ioc_arg_parser, run

    args = _parse_args(sys.argv)
    prefix = args.prefix.rstrip(":")

    print(f"[gige] camera {args.camera_ip}  ->  {prefix}:")
    driver = GigEDriver(args.camera_ip, args.gentl_path)

    IOCClass = build_ioc_class(GigEDriver)
    ioc_options, run_options = ioc_arg_parser(
        default_prefix=f"{prefix}:",
        desc=f"GigE Vision IOC: {prefix} -> {args.camera_ip}")
    ioc = IOCClass(driver=driver, **ioc_options)

    async def startup_hook(async_lib):
        await ioc.startup()
        if args.rate > 0:
            await ioc.cam1_AcquirePeriod.write(1.0 / args.rate)
            await ioc.cam1_ImageMode.write(ImageMode.Continuous)
            await ioc.cam1_Acquire.write(AcquireState.Acquire)
            print(f"[gige] continuous acquisition started at {args.rate} Hz")

    run(ioc.pvdb, startup_hook=startup_hook, **run_options)


if __name__ == "__main__":
    main()
