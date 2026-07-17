"""
aravis_ioc.py — standalone IOC for GigE Vision cameras via Aravis.

Built on Aravis (a vendor-neutral GigE Vision implementation) rather than
a vendor GenTL producer, so it runs on machines too old for modern vendor
SDKs (vendor .cti files are binaries with glibc requirements; Aravis can be
compiled into a conda env on anything, glibc 2.17 included).  This is THE
GigE IOC — the older vendor-GenTL gige_ioc.py is retired in attic/.

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
    python aravis_ioc.py CAMERA_IP PREFIX [--rate HZ] [--swap-endian]
                         [caproto options, e.g. --list-pvs]

--swap-endian: some cameras send >8-bit pixel data big-endian and Aravis
assumes little-endian (FLIR/Point Grey Blackfly, e.g. BFLY-PGE-31S4M —
https://github.com/AravisProject/aravis/issues/921, still open; the
camera's own pgrPixelBigEndian feature doesn't help).  The symptom is
noise-like images with wild banding.  It's up to whoever runs the IOC to
know their camera needs this.

Calibration persistence: cam1:CalibX/Y (um/pixel) are saved to
.calib_<PREFIX>.json in the directory the IOC is launched from (cwd, not
the script dir — the IOC user may not have write permission there) on
every write and reloaded at startup, so the viewscreen scale survives
IOC restarts.  Launch from the same directory to keep the calibration.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

import numpy as np

from ad_ioc_base import AcquireState, CameraDriver, ImageMode, build_ioc_class

#: number of buffers in the stream ring; capture() drains to the newest so
#: a slow consumer sees fresh frames, not the oldest queued one
N_STREAM_BUFFERS = 4


class AravisDriver(CameraDriver):
    manufacturer = "GigE Vision"
    extension_pvs: list = []

    def __init__(self, ip_address: str, swap_endian: bool = False,
                 calib_file: str | None = None):
        import gi
        gi.require_version("Aravis", "0.8")
        from gi.repository import Aravis
        self._Aravis = Aravis

        # Some cameras deliver >8-bit pixel data big-endian while Aravis
        # assumes little-endian (Blackfly; aravis issue #921). User-declared
        # at startup — there's no reliable way to probe it.
        self._swap_endian = bool(swap_endian)

        # cam1:CalibX/Y (um/px) persistence: a tiny JSON file keyed by the
        # served EPICS prefix (not the camera IP — some cameras get DHCP
        # addresses). Loaded by ad_ioc_base at startup, rewritten on every
        # calibration put. None = PV-only, lost on restart.
        self._calib_file = Path(calib_file) if calib_file else None

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
        selected = None
        for fmt, bits in [("Mono16", 16), ("Mono12", 12),
                          ("Mono10", 10), ("Mono8", 8)]:
            try:
                self._cam.set_pixel_format_from_string(fmt)
                self._bits = bits
                selected = fmt
                break
            except Exception:
                continue
        if selected is None:
            # No unpacked mono format — a color camera, or one offering only
            # packed mono (Mono10p etc.). This driver decodes only unpacked
            # mono, so warn loudly with the available formats; capture() will
            # fail-soft (skip frames) rather than stream garbage or crash.
            try:
                avail = self._cam.dup_available_pixel_formats_as_strings()
            except Exception:
                avail = ["<unavailable>"]
            print("[aravis] WARNING: no unpacked mono format on this camera "
                  f"(available: {', '.join(avail)}). This driver only handles "
                  "unpacked mono (Mono8/10/12/16) — images will not decode. "
                  "Color/packed support is not implemented.")

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

        self._apply_startup_defaults()

        self._sensor_w, self._sensor_h = self._cam.get_sensor_size()
        self._stream = None
        self._streaming = False
        # Auto-recovery state: GigE streams occasionally wedge into a state
        # where every buffer comes back failed; a stream restart clears it.
        self._consec_errors = 0
        self._last_restart = 0.0
        # Aravis calls are serialized: the acquisition loop and PV putters
        # run on different worker threads
        self._lock = threading.Lock()

        swap_note = ", swapping pixel endianness" if self._swap_endian else ""
        print(f"[aravis] connected: {self.manufacturer} {self.model}, "
              f"{self._sensor_w}x{self._sensor_h}, Mono{self._bits}{swap_note}")

    def _apply_startup_defaults(self):
        """Put the camera in plain manual-exposure mode.

        Without this, a camera left in auto-exposure (ExposureAuto =
        Continuous) silently ignores ExposureTime writes — the classic
        "can't change the exposure" symptom.  We also disable auto gain and,
        where present, the frame-rate cap that would otherwise limit the
        maximum exposure time.  Everything is best-effort: features vary by
        vendor, and a missing one just means it didn't need setting.
        """
        Aravis = self._Aravis

        # Auto exposure / gain off (high-level API maps to the right node)
        for label, fn, arg in [
            ("exposure auto", self._cam.set_exposure_time_auto, Aravis.Auto.OFF),
            ("gain auto", self._cam.set_gain_auto, Aravis.Auto.OFF),
        ]:
            try:
                fn(arg)
            except Exception as e:
                print(f"[aravis] {label}: not set ({e})")

        # ExposureMode must be Timed for ExposureTime to apply (some cameras
        # default to TriggerWidth etc.); TriggerMode off = free-running.
        # The frame-period cap on long exposures is handled in the exposure
        # setter via set_frame_rate, not here. Generic-node, best-effort.
        dev = self._cam.get_device()
        for feature, value in [("ExposureMode", "Timed"),
                               ("TriggerMode", "Off")]:
            try:
                dev.set_string_feature_value(feature, value)
            except Exception:
                pass  # feature absent on this model — fine

        try:
            exp = self._cam.get_exposure_time()
            mn, mx = self._cam.get_exposure_time_bounds()
            print(f"[aravis] manual exposure mode: {exp:.0f} us "
                  f"(range {mn:.0f}-{mx:.0f} us)")
        except Exception:
            pass

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
        """Apply the ROI feature-by-feature, NOT via Camera.set_region().

        set_region() writes OffsetX=0, OffsetY=0, Width, Height, OffsetX,
        OffsetY with no snapping to the camera's GenICam increments and
        aborts on the first rejected write — so a single misaligned value
        (a zoomed beamview selection is four arbitrary numbers) strands
        the offsets at 0.  Snapping each value to its own bounds/increment
        and writing the features independently lands any request on the
        nearest legal region instead.
        """
        with self._lock:
            was_streaming = self._streaming
            if was_streaming:
                self._stop_stream()
            dev = self._cam.get_device()
            try:
                # Offsets to 0 first so any new size fits; sizes next; the
                # offset bounds then already account for the new sizes.
                self._set_int_feature(dev, "OffsetX", 0)
                self._set_int_feature(dev, "OffsetY", 0)
                self._set_int_feature(dev, "Width", int(w))
                self._set_int_feature(dev, "Height", int(h))
                self._set_int_feature(dev, "OffsetX", int(x))
                self._set_int_feature(dev, "OffsetY", int(y))
            finally:
                if was_streaming:
                    self._start_stream()
        return self.get_roi()

    @staticmethod
    def _set_int_feature(dev, name, value):
        """Write one integer GenICam feature, snapped to its current
        min/max/increment (cameras hard-reject misaligned values).
        Best-effort: a failure is printed, not raised, so the remaining
        region features still get applied."""
        v = int(value)
        try:
            try:
                mn, mx = dev.get_integer_feature_bounds(name)
                v = max(int(mn), min(v, int(mx)))
            except Exception:
                mn = 0
            try:
                inc = max(1, int(dev.get_integer_feature_increment(name)))
            except Exception:
                inc = 1
            # round down onto the legal grid (min + k*inc); never up, so
            # offset+size can't creep past the sensor edge
            v = int(mn) + ((v - int(mn)) // inc) * inc
            dev.set_integer_feature_value(name, v)
        except Exception as e:
            print(f"[aravis] roi {name}={value}: {e}")

    # -- calibration persistence ---------------------------------------------------

    def load_calibration(self):
        f = self._calib_file
        if f is None or not f.exists():
            return None
        try:
            d = json.loads(f.read_text())
            return float(d["calib_x_um"]), float(d["calib_y_um"])
        except Exception as e:
            print(f"[aravis] calibration load ({f}): {e}")
            return None

    def save_calibration(self, cal_x_um, cal_y_um):
        f = self._calib_file
        if f is None:
            return
        try:
            tmp = f.with_name(f.name + ".tmp")
            tmp.write_text(json.dumps({"calib_x_um": float(cal_x_um),
                                       "calib_y_um": float(cal_y_um)},
                                      indent=2) + "\n")
            tmp.replace(f)   # atomic-ish: no torn file on a crash mid-write
        except Exception as e:
            print(f"[aravis] calibration save ({f}): {e}")

    # -- exposure / gain ------------------------------------------------------------

    @property
    def exposure_time(self) -> float:
        with self._lock:
            return float(self._cam.get_exposure_time()) / 1e6  # us -> s

    @exposure_time.setter
    def exposure_time(self, seconds: float):
        with self._lock:
            us = seconds * 1e6
            # A camera can't expose longer than its frame period, so the
            # exposure max is bounded by the frame rate. Set the hardware
            # frame rate to fit the requested exposure (5% overhead), capped
            # at the camera's max rate so short exposures still run fast.
            # set_frame_rate enables frame-rate control internally and is
            # vendor-aware (better than poking AcquisitionFrameRateEnable).
            try:
                _fr_min, fr_max = self._cam.get_frame_rate_bounds()
                target = fr_max if us <= 0 else min(fr_max, 1e6 / (us * 1.05))
                target = max(_fr_min, target)
                self._cam.set_frame_rate(target)
            except Exception as e:
                print(f"[aravis] frame-rate for exposure: {e}")
            # Now bounds reflect the new frame rate; clamp into them
            try:
                mn, mx = self._cam.get_exposure_time_bounds()
                us = max(mn, min(mx, us))
            except Exception:
                pass
            try:
                self._cam.set_exposure_time(us)
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

    def _restart_stream(self):
        """Tear down and recreate the stream (call with lock held).

        Recovery for the GigE failure mode where every buffer comes back
        with a non-success status until acquisition is cycled — the same
        stop/start a user would do by hand, done automatically."""
        import time
        self._stop_stream()
        time.sleep(0.2)
        self._start_stream()
        self._last_restart = time.time()
        self._consec_errors = 0
        print("[aravis] stream restarted (auto-recovery)")

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

                status = buf.get_status()
                if status != Aravis.BufferStatus.SUCCESS:
                    w = h = 0
                else:
                    self._consec_errors = 0
                    w = int(buf.get_image_width())
                    h = int(buf.get_image_height())
                    data = buf.get_data()   # bytes, copied by pygobject
                # Return the buffer to its own stream BEFORE any restart, so
                # we never push a stale buffer onto a freshly-recreated stream
                self._stream.push_buffer(buf)

                if status != Aravis.BufferStatus.SUCCESS:
                    self._consec_errors += 1
                    if self._consec_errors <= 3:   # don't spam a long run
                        print(f"[aravis] buffer status {status}; skipping frame")
                    # Sustained failure + not just-restarted -> auto-recover.
                    # ~30 consecutive bad buffers, at most one restart/10 s.
                    import time
                    if (self._consec_errors >= 30 and
                            time.time() - self._last_restart > 10.0):
                        self._restart_stream()
                    return None
            finally:
                if started_here:
                    self._stop_stream()

        # Decode fail-soft: if the buffer doesn't match an unpacked-mono
        # w*h layout (unexpected/packed/color format), skip the frame with a
        # throttled warning rather than letting the exception stop the
        # acquisition loop.
        try:
            bytes_per_px = 1 if self._bits == 8 else 2
            need = w * h * bytes_per_px
            if len(data) < need:
                raise ValueError(
                    f"buffer {len(data)} bytes < {need} for {w}x{h} "
                    f"Mono{self._bits}")
            if self._bits == 8:
                img = np.frombuffer(data, dtype=np.uint8, count=w * h)
                return img.reshape(h, w).astype(np.uint16)
            # --swap-endian: parse as big-endian and let the conversion to
            # native uint16 do the byte swap (one C loop, no extra pass)
            wire = ">u2" if self._swap_endian else "<u2"
            img = np.frombuffer(data, dtype=wire, count=w * h)
            return np.ascontiguousarray(img.reshape(h, w), dtype=np.uint16)
        except Exception as e:
            self._decode_errors = getattr(self, "_decode_errors", 0) + 1
            if self._decode_errors <= 3:
                print(f"[aravis] frame decode failed ({e}); skipping. "
                      "Wrong pixel format for this driver?")
            return None

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
    parser.add_argument(
        "--swap-endian", "--swap_endian", action="store_true",
        help="Byte-swap >8-bit pixel data before serving. Needed for cameras "
             "that send big-endian while Aravis assumes little-endian "
             "(FLIR/Point Grey Blackfly — aravis issue #921)",
    )
    args, remaining = parser.parse_known_args(argv[1:])
    sys.argv = [argv[0], *remaining]
    return args


def main():
    from caproto.server import ioc_arg_parser, run

    args = _parse_args(sys.argv)
    prefix = args.prefix.rstrip(":")

    print(f"[aravis] camera {args.camera_ip}  ->  {prefix}:")

    # Persist cam1:CalibX/Y in the directory the IOC is launched from
    # (the script dir may not be writable by the IOC user), keyed by
    # prefix so the calibration follows the camera name, not a
    # (possibly DHCP) IP.
    calib_file = Path.cwd() / f".calib_{prefix}.json"
    state = "found" if calib_file.exists() else "created on first write"
    print(f"[aravis] calibration file: {calib_file} ({state})")

    driver = AravisDriver(args.camera_ip, swap_endian=args.swap_endian,
                          calib_file=calib_file)

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
