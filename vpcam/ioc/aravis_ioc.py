"""
aravis_ioc.py — standalone IOC for GigE Vision cameras via Aravis.

Same job and same CLI shape as gige_ioc.py, but built on Aravis (a
vendor-neutral GigE Vision implementation) instead of a vendor GenTL
producer + harvesters.  Exists for machines too old to run modern vendor
SDKs (vendor .cti files are binaries with glibc requirements; Aravis can be
compiled into a conda env on anything, glibc 2.17 included).

    python aravis_ioc.py 192.168.136.23 B24Screen1
        -> serves B24Screen1:cam1:..., B24Screen1:image1:...

A nice Aravis perk: connection is by unicast to the given IP, with no
broadcast discovery step — so it can even reach a camera across a routed
subnet, where GenTL producers see nothing.

Build recipe (inside a conda env, no root — battle-tested on SL 7.9,
glibc 2.17, 2026-06-11):

    # Run ALL mamba installs from the OLDEST machine that shares the env:
    # the solver checks glibc compatibility against the machine it runs on,
    # so installs from a newer machine can pull packages the old one can't load.
    mamba install -c conda-forge compilers meson ninja pkg-config glib \\
        libxml2-devel gobject-introspection pygobject "sysroot_linux-64=2.17"
    #   libxml2-devel: conda-forge split the .pc/headers out of libxml2
    #   sysroot 2.17:  conda compilers default to a newer glibc baseline;
    #                  this pin makes builds run on old hosts (and makes the
    #                  build machine irrelevant — it cross-targets 2.17)

    curl -LO https://github.com/AravisProject/aravis/releases/download/0.8.35/aravis-0.8.35.tar.xz
    tar xf aravis-0.8.35.tar.xz && cd aravis-0.8.35
    export PKG_CONFIG_PATH=$CONDA_PREFIX/lib/pkgconfig:$CONDA_PREFIX/share/pkgconfig
    meson setup build --prefix=$CONDA_PREFIX -Dgst-plugin=disabled \\
        -Dviewer=disabled -Dusb=disabled
    ninja -C build install

    # On RHEL-family hosts meson installs to lib64/ — make every future
    # shell find the library and typelib automatically:
    mkdir -p $CONDA_PREFIX/etc/conda/activate.d
    cat > $CONDA_PREFIX/etc/conda/activate.d/aravis.sh <<'EOF'
    export LD_LIBRARY_PATH=$CONDA_PREFIX/lib64:$CONDA_PREFIX/lib:$LD_LIBRARY_PATH
    export GI_TYPELIB_PATH=$CONDA_PREFIX/lib64/girepository-1.0
    EOF

Then `arv-tool-0.8` lists cameras, and this IOC runs.

Usage:
    python aravis_ioc.py CAMERA_IP PREFIX [--rate HZ]
                         [caproto options, e.g. --list-pvs]
"""

from __future__ import annotations

import argparse
import sys
import threading

import numpy as np

from ad_ioc_base import AcquireState, CameraDriver, ImageMode, build_ioc_class

#: number of buffers in the stream ring; capture() drains to the newest so
#: a slow consumer sees fresh frames, not the oldest queued one
N_STREAM_BUFFERS = 4


class AravisDriver(CameraDriver):
    manufacturer = "GigE Vision"
    extension_pvs: list = []

    def __init__(self, ip_address: str):
        import gi
        gi.require_version("Aravis", "0.8")
        from gi.repository import Aravis
        self._Aravis = Aravis

        # Direct unicast connection — no discovery broadcast involved
        self._cam = Aravis.Camera.new(ip_address)
        try:
            type(self).manufacturer = str(self._cam.get_vendor_name())
            type(self).model = str(self._cam.get_model_name())
        except Exception:
            type(self).model = f"GigE camera @ {ip_address}"

        # Largest unpacked mono format the camera offers (PFNC formats are
        # right-aligned, so no bit shifting is needed on the data)
        self._bits = 8
        for fmt, bits in [("Mono16", 16), ("Mono12", 12),
                          ("Mono10", 10), ("Mono8", 8)]:
            try:
                self._cam.set_pixel_format_from_string(fmt)
                self._bits = bits
                break
            except Exception:
                continue

        try:
            self._cam.set_acquisition_mode(Aravis.AcquisitionMode.CONTINUOUS)
        except Exception:
            pass
        try:
            # Negotiate GVSP packet size for the actual path MTU — important
            # on plain-1500-MTU NICs (USB adapters etc.)
            self._cam.gv_auto_packet_size()
        except Exception:
            pass

        self._sensor_w, self._sensor_h = self._cam.get_sensor_size()
        self._stream = None
        self._streaming = False
        # Aravis calls are serialized: the acquisition loop and PV putters
        # run on different worker threads
        self._lock = threading.Lock()

        print(f"[aravis] connected: {self.manufacturer} {self.model}, "
              f"{self._sensor_w}x{self._sensor_h}, Mono{self._bits}")

    # -- geometry ----------------------------------------------------------------

    @property
    def sensor_width(self) -> int:
        return int(self._sensor_w)

    @property
    def sensor_height(self) -> int:
        return int(self._sensor_h)

    def get_roi(self):
        with self._lock:
            x, y, w, h = self._cam.get_region()
        return int(x), int(y), int(w), int(h)

    def set_roi(self, x, y, w, h):
        with self._lock:
            was_streaming = self._streaming
            if was_streaming:
                self._stop_stream()
            try:
                self._cam.set_region(int(x), int(y), int(w), int(h))
            except Exception as e:
                print(f"[aravis] roi: {e}")
            finally:
                if was_streaming:
                    self._start_stream()
        return self.get_roi()

    # -- exposure / gain ------------------------------------------------------------

    @property
    def exposure_time(self) -> float:
        with self._lock:
            return float(self._cam.get_exposure_time()) / 1e6  # us -> s

    @exposure_time.setter
    def exposure_time(self, seconds: float):
        with self._lock:
            try:
                self._cam.set_exposure_time(seconds * 1e6)
            except Exception as e:
                print(f"[aravis] exposure: {e}")

    @property
    def gain(self) -> float:
        with self._lock:
            try:
                return float(self._cam.get_gain())
            except Exception:
                return 0.0

    @gain.setter
    def gain(self, value: float):
        with self._lock:
            try:
                self._cam.set_gain(float(value))
            except Exception as e:
                print(f"[aravis] gain: {e}")

    @property
    def bits_per_pixel(self) -> int:
        return self._bits

    # -- acquisition -------------------------------------------------------------------

    def _start_stream(self):
        """Create the stream and start acquisition (call with lock held)."""
        Aravis = self._Aravis
        self._stream = self._cam.create_stream(None, None)
        payload = self._cam.get_payload()
        for _ in range(N_STREAM_BUFFERS):
            self._stream.push_buffer(Aravis.Buffer.new_allocate(payload))
        self._cam.start_acquisition()
        self._streaming = True

    def _stop_stream(self):
        """Stop acquisition and drop the stream (call with lock held)."""
        try:
            self._cam.stop_acquisition()
        except Exception:
            pass
        self._stream = None   # buffers are owned by the stream; let it go
        self._streaming = False

    def on_acquire_start(self):
        with self._lock:
            if not self._streaming:
                self._start_stream()

    def on_acquire_stop(self):
        with self._lock:
            if self._streaming:
                self._stop_stream()

    def capture(self):
        Aravis = self._Aravis
        with self._lock:
            started_here = not self._streaming
            if started_here:
                self._start_stream()
            try:
                buf = self._stream.timeout_pop_buffer(5_000_000)  # us
                if buf is None:
                    return None   # no frame this time; loop retries
                # Drain to the newest completed buffer so a slow relay/client
                # never displays stale queued frames
                while True:
                    nxt = self._stream.try_pop_buffer()
                    if nxt is None:
                        break
                    self._stream.push_buffer(buf)
                    buf = nxt

                try:
                    if buf.get_status() != Aravis.BufferStatus.SUCCESS:
                        print(f"[aravis] buffer status {buf.get_status()}; "
                              "skipping frame")
                        return None
                    w = int(buf.get_image_width())
                    h = int(buf.get_image_height())
                    data = buf.get_data()   # bytes, copied by pygobject
                finally:
                    self._stream.push_buffer(buf)
            finally:
                if started_here:
                    self._stop_stream()

        if self._bits == 8:
            img = np.frombuffer(data, dtype=np.uint8, count=w * h)
            return img.reshape(h, w).astype(np.uint16)
        img = np.frombuffer(data, dtype="<u2", count=w * h)
        return np.ascontiguousarray(img.reshape(h, w))

    def close(self):
        try:
            self.on_acquire_stop()
        except Exception:
            pass
        self._cam = None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args(argv):
    parser = argparse.ArgumentParser(
        add_help=False,
        description="Standalone IOC for a GigE Vision camera (via Aravis)",
    )
    parser.add_argument(
        "camera_ip",
        help="Camera IP address, e.g. 192.168.136.23 (unicast — no "
             "broadcast discovery needed)",
    )
    parser.add_argument(
        "prefix",
        help="PV prefix to serve, e.g. B24Screen1",
    )
    parser.add_argument(
        "--rate", type=float, default=0.0,
        help="Start continuous acquisition at this rate on boot, Hz "
             "(default 0 = boot idle; clients start via cam1:Acquire)",
    )
    args, remaining = parser.parse_known_args(argv[1:])
    sys.argv = [argv[0], *remaining]
    return args


def main():
    from caproto.server import ioc_arg_parser, run

    args = _parse_args(sys.argv)
    prefix = args.prefix.rstrip(":")

    print(f"[aravis] camera {args.camera_ip}  ->  {prefix}:")
    driver = AravisDriver(args.camera_ip)

    IOCClass = build_ioc_class(AravisDriver)
    ioc_options, run_options = ioc_arg_parser(
        default_prefix=f"{prefix}:",
        desc=f"Aravis GigE Vision IOC: {prefix} -> {args.camera_ip}")
    ioc = IOCClass(driver=driver, **ioc_options)

    async def startup_hook(async_lib):
        await ioc.startup()
        if args.rate > 0:
            await ioc.cam1_AcquirePeriod.write(1.0 / args.rate)
            await ioc.cam1_ImageMode.write(ImageMode.Continuous)
            await ioc.cam1_Acquire.write(AcquireState.Acquire)
            print(f"[aravis] continuous acquisition started at {args.rate} Hz")

    run(ioc.pvdb, startup_hook=startup_hook, **run_options)


if __name__ == "__main__":
    main()
