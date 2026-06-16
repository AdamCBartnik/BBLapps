"""
empad_ioc.py — standalone areaDetector-style IOC for the EMPAD detector.

EMPAD is a two-image ("double") pump/probe detector. This IOC serves the
ad_ioc_base contract (so beamview and any areaDetector client talk to it
exactly like the Pi/GigE cameras) and REPLACES two legacy pieces:
  - the separate C soft-IOC that served the old GC_* / cyclepump records, and
  - python_epics_ioc.py, the ramdisk->EPICS image publisher.

It deliberately depends ONLY on ad_ioc_base (drop that file alongside this one
on the EMPAD box). It does NOT talk to camserver: the existing python_ioc.py
keeps doing the camserver socket + trigger choreography and writes raw frames
to the ramdisk; this IOC's driver watches for those files and publishes them.

Data flow (per acquisition):
    camserver --(python_ioc.py)--> /tmp/ramdisk/im_x<N>.raw  (float32,
        N frames of (HEIGHT+2) x WIDTH; the +2 rows are metadata)
    EMPADDriver.capture(): on a new file, sum the N frames, optionally
        background-subtract / threshold (per sub-frame, BEFORE summing, exactly
        as the legacy did), split pump/probe by frame parity, and return
        (cold, hot) = (image1, image2). beamview forms Normal/Cold/Hot/Diff.

Served as Int32 (sums exceed uint16 and go negative after bg-subtract), matching
the legacy .astype(int) behavior.

Usage:
    python empad_ioc.py                 -> serves EMPAD:cam1:..., EMPAD:image1:...,
                                           EMPAD:image2:...
    python empad_ioc.py --prefix EMPAD --ramdisk /tmp/ramdisk
    python empad_ioc.py --list-pvs

cyclepump is removed: image1 (cold) and image2 (hot) are always produced (the
two interleaved sub-sets when n_frames is even). The old cyclepump==2 "8xN table
montage" is not produced; if ever wanted it would become a separate image3
surface (parked — beamview can't read image3 yet, and its size varies with
n_frames so it needs its own max buffer).
"""

from __future__ import annotations

import argparse
import os
import sys
import time

os.environ.setdefault("EPICS_CA_MAX_ARRAY_BYTES", "40000000")

import numpy as np

from ad_ioc_base import (AcquireState, ADColorMode, ADDataType, CameraDriver,
                         ExtensionPV, ImageMode, build_ioc_class)

# Detector geometry. The raw frame has EXTRA_ROWS extra rows of metadata
# appended below the image; the last row carries the frame counter used for
# pump/probe parity.
WIDTH = 128
HEIGHT = 128
EXTRA_ROWS = 2
RAW_ROWS = HEIGHT + EXTRA_ROWS

DEFAULT_RAMDISK = "/tmp/ramdisk"
RAWFILE_STEM = "im"          # files are "<stem>_x<n_frames>.raw"
FILE_POLL = 0.01

# Summed Int32 counts have no clean bit depth; this only sets beamview's
# initial display ceiling (the user auto-ranges anyway).
DEFAULT_BITS = 20


def _clamp_roi(x, y, w, h, max_w, max_h):
    x = max(0, min(int(x), max_w - 1))
    y = max(0, min(int(y), max_h - 1))
    w = max(1, min(int(w), max_w - x))
    h = max(1, min(int(h), max_h - y))
    return x, y, w, h


def process_raw(data, n_frames, roi, *, bg=None,
                subtract_bg=False, threshold=0.0, threshold_enable=False,
                save_bg=False):
    """Turn one raw acquisition into (cold, hot[, new_bg]).

    Ported from python_epics_ioc.py so the pixel math is identical: reshape to
    [N/states, states, RAW_ROWS, WIDTH], optional pre-sum background-subtract +
    threshold, sum over frames, strip the metadata rows, crop to the ROI, and
    split pump/probe by parity.

    cyclepump is GONE: all images are always produced. When n_frames is even we
    always split into the two interleaved sub-sets (cold=image1, hot=image2);
    when not actually pump/probing the halves are ~equal (Normal=full sum is
    still correct, Diff is just noise). Odd n_frames -> single image, hot=None.

    data: 1-D float array, length RAW_ROWS*WIDTH*n_frames (raw file contents).
    Returns (cold, hot, new_bg): hot is None in single mode; new_bg is a 2-D
    array when save_bg requested this frame, else None.
    """
    x, y, w, h = roi
    expected = RAW_ROWS * WIDTH * n_frames
    if data.size != expected:
        raise ValueError(f"raw size {data.size} != expected {expected} "
                         f"(n_frames={n_frames})")

    nstates = 2 if n_frames % 2 == 0 else 1
    raw = data.reshape(n_frames // nstates, nstates, RAW_ROWS, WIDTH)
    frame_count = raw[0, 0, -1, 1]
    parity = int(frame_count % 2)

    work = raw.astype(np.float64, copy=True)
    if subtract_bg and bg is not None:
        work = work - bg[np.newaxis, np.newaxis, :, :]
    if threshold_enable and threshold > 0:
        work[work < threshold] = 0.0

    new_bg = None
    if save_bg:
        new_bg = np.mean(np.mean(raw, axis=0), axis=0)   # 2-D (RAW_ROWS, WIDTH)

    # Strip the metadata rows and swap to (frames, states, WIDTH, HEIGHT)
    work = np.transpose(work[:, :, :-EXTRA_ROWS, :], [0, 1, 3, 2])
    summed = np.sum(work, axis=0)                        # (states, WIDTH, HEIGHT)
    summed = summed[:, x:x + w, y:y + h]                 # crop to ROI

    if nstates == 2:
        hot = summed[parity]
        cold = summed[1 - parity]
    else:
        cold = np.sum(summed, axis=0)                    # single image
        hot = None
    return cold, hot, new_bg


# Extension-PV accessors (module-level so they can sit in extension_pvs).

def _set_n_frames(d, v):
    d._n_frames = max(1, int(round(v)))
    return d._n_frames


def _set_trigger_sleep(d, v):
    d._trigger_sleep = max(0.0, float(v))
    return d._trigger_sleep


def _set_status(d, v):
    d._status = str(v)
    return d._status


def _set_threshold(d, v):
    d._threshold = float(v)
    return d._threshold


def _set_threshold_enable(d, v):
    d._threshold_enable = int(bool(round(v)))
    return d._threshold_enable


def _set_subtract_bg(d, v):
    d._subtract_bg = int(bool(round(v)))
    return d._subtract_bg


def _set_save_bg(d, v):
    d._save_bg = int(bool(round(v)))
    return d._save_bg


class EMPADDriver(CameraDriver):
    """Ramdisk-file-watching driver for the EMPAD detector.

    capture() is event-driven (returns None until a new raw file appears, like
    the gateway driver), so the ad_ioc_base acquisition loop publishes one
    frame per detector acquisition. camserver control lives in python_ioc.py.
    """

    manufacturer = "Cornell"
    model = "EMPAD"
    dual_frame = True
    data_type = ADDataType.Int32          # class attr: shadows the base property

    extension_pvs = [
        ExtensionPV(name="n_frames", dtype=int, initial=64,
                    doc="Frames summed per acquisition",
                    getter=lambda d: d._n_frames, setter=_set_n_frames),
        ExtensionPV(name="trigger_sleep", dtype=float, initial=0.02,
                    doc="Trigger settle time (s); read by python_ioc.py",
                    getter=lambda d: d._trigger_sleep, setter=_set_trigger_sleep),
        ExtensionPV(name="Status", dtype=str, initial="",
                    doc="IOC/acquisition status string (set by python_ioc.py)",
                    getter=lambda d: d._status, setter=_set_status),
        ExtensionPV(name="hw_threshold", dtype=float, initial=0.0,
                    doc="Per-sub-frame threshold (applied before summing)",
                    getter=lambda d: d._threshold, setter=_set_threshold),
        ExtensionPV(name="hw_threshold_enable", dtype=int, initial=0,
                    doc="Enable hw_threshold", getter=lambda d: d._threshold_enable,
                    setter=_set_threshold_enable),
        ExtensionPV(name="hw_subtract_bg", dtype=int, initial=0,
                    doc="Subtract stored background (before summing)",
                    getter=lambda d: d._subtract_bg, setter=_set_subtract_bg),
        ExtensionPV(name="hw_save_bg", dtype=int, initial=0,
                    doc="Write 1 to capture the next frame as background; "
                        "auto-clears", getter=lambda d: d._save_bg,
                    setter=_set_save_bg, poll_period=1.0),
    ]

    def __init__(self, ramdisk: str = DEFAULT_RAMDISK):
        self._ramdisk = ramdisk
        self._roi = (0, 0, WIDTH, HEIGHT)
        self._exposure = 0.000998
        self._n_frames = 64
        self._trigger_sleep = 0.02
        self._status = ""
        self._threshold = 0.0
        self._threshold_enable = 0
        self._subtract_bg = 0
        self._save_bg = 0
        self._bg = None                       # 2-D (RAW_ROWS, WIDTH) float
        self._last_file_t = None

    # -- geometry -----------------------------------------------------------

    @property
    def sensor_width(self):
        return WIDTH

    @property
    def sensor_height(self):
        return HEIGHT

    def get_roi(self):
        return self._roi

    def set_roi(self, x, y, w, h):
        self._roi = _clamp_roi(x, y, w, h, WIDTH, HEIGHT)
        return self._roi

    # -- exposure / gain ----------------------------------------------------

    @property
    def exposure_time(self):
        return self._exposure

    @exposure_time.setter
    def exposure_time(self, seconds):
        # Stored only; python_ioc.py reads cam1:AcquireTime and forwards it to
        # camserver via Set_Take_N.
        self._exposure = max(1e-6, float(seconds))

    @property
    def gain(self):
        return 1.0

    @gain.setter
    def gain(self, value):
        pass                                  # EMPAD has no analogue gain

    @property
    def bits_per_pixel(self):
        return DEFAULT_BITS

    # -- acquisition (file watch) -------------------------------------------

    def _rawfile(self):
        return os.path.join(self._ramdisk,
                            f"{RAWFILE_STEM}_x{int(self._n_frames)}.raw")

    def on_acquire_start(self):
        # Baseline the file time so we only publish frames written after start.
        try:
            self._last_file_t = os.stat(self._rawfile()).st_mtime
        except OSError:
            self._last_file_t = None

    def capture(self):
        """Return (cold, hot) when a new raw file is present, else None."""
        path = self._rawfile()
        try:
            file_t = os.stat(path).st_mtime
        except OSError:
            time.sleep(FILE_POLL)
            return None
        if self._last_file_t is not None and file_t <= self._last_file_t:
            time.sleep(FILE_POLL)
            return None                       # no new acquisition yet

        # Read (np.fromfile copies into memory; camserver may still hold the
        # file, but it is written atomically per acquisition).
        try:
            data = np.fromfile(path, dtype=np.float32)
        except OSError:
            return None
        if data.size != RAW_ROWS * WIDTH * int(self._n_frames):
            return None                       # partial/old file; retry

        cold, hot, new_bg = process_raw(
            data, int(self._n_frames), self._roi,
            bg=self._bg, subtract_bg=bool(self._subtract_bg),
            threshold=float(self._threshold),
            threshold_enable=bool(self._threshold_enable),
            save_bg=bool(self._save_bg))

        if new_bg is not None:
            self._bg = new_bg
            self._save_bg = 0                 # auto-clear, like the legacy IOC
        if hot is None:
            hot = np.zeros_like(cold)         # single mode: image2 = zeros

        self._last_file_t = file_t
        return cold, hot


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        add_help=False, description="Standalone areaDetector-style EMPAD IOC")
    parser.add_argument("--prefix", default="EMPAD",
                        help="PV prefix to serve (default: EMPAD)")
    parser.add_argument("--ramdisk", default=DEFAULT_RAMDISK,
                        help=f"Raw-frame directory (default: {DEFAULT_RAMDISK})")
    args, remaining = parser.parse_known_args(argv[1:])
    sys.argv = [argv[0], *remaining]
    return args


def main():
    from caproto.server import ioc_arg_parser, run

    args = _parse_args(sys.argv)
    prefix = args.prefix.rstrip(":")
    print(f"[empad] EMPAD IOC -> {prefix}:  (ramdisk {args.ramdisk})")

    driver = EMPADDriver(ramdisk=args.ramdisk)
    IOCClass = build_ioc_class(EMPADDriver)
    ioc_options, run_options = ioc_arg_parser(
        default_prefix=f"{prefix}:", desc=f"EMPAD areaDetector IOC ({prefix})")
    ioc = IOCClass(driver=driver, **ioc_options)

    async def startup_hook(async_lib):
        await ioc.startup()
        # Boot acquiring: the loop publishes whenever python_ioc.py produces a
        # new raw file. cam1:Acquire still gates it.
        await ioc.cam1_ImageMode.write(ImageMode.Continuous)
        await ioc.cam1_Acquire.write(AcquireState.Acquire)
        print("[empad] watching ramdisk for new acquisitions")

    run(ioc.pvdb, startup_hook=startup_hook, **run_options)


if __name__ == "__main__":
    main()
