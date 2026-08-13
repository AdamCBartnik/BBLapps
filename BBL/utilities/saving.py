r"""
ssss() -- "see something save something", the notebook counterpart to
beamview's Save button.

    import BBL as bbl

    bbl.plot_frame(bbl.get_frame('B24Screen1'))
    bbl.ssss()                       # -> ssss_001.png in today's data dir

    ax = bbl.plot_frame(frame)
    bbl.ssss(ax, data=frame)         # -> ssss_002.png AND ssss_002.h5

    bbl.ssss(name='solenoid_scan')   # -> solenoid_scan.png (then _2, _3, ...)

Files land in get_todays_directory() and are numbered the same way
ssss.m and beamview number theirs.  The numbering is SHARED with
beamview: a stem counts as taken if either its .png or its .h5 exists,
so notebook figures and beamview snapshots interleave into one sequence
instead of overwriting each other.

next_ssss_stem() below is the single implementation of that numbering:
beamview/snapshot_window.py imports it from here (it already imports
BBL.today for the directory itself), so the two tools cannot drift into
disagreeing about which numbers are taken.

The optional `data` dict is written alongside the PNG as HDF5.  Nothing
is inferred from the figure -- a half-recovered record that looks
complete is worse than no record -- so pass the data explicitly.  A
get_frame() dict is already in beamview's snapshot schema, so

    bbl.ssss(ax, data=frame)
    frame2 = bbl.get_frame('ssss_002.h5')    # reads straight back

round-trips.  Any other dict works too (see _write_h5 for the
dataset-vs-attribute rule); it just won't be loadable by get_frame,
which requires image/xx/yy.
"""
from pathlib import Path

import numpy as np

from .today import get_todays_directory

# A stem is TAKEN if either extension exists -- keeps notebook figures and
# beamview snapshots on one shared numbering sequence.
_EXTS = (".png", ".h5")

# Written as HDF5 datasets (compressed); everything else in a data dict
# becomes an attribute, except arrays too large to be one.  This
# reproduces beamview's snapshot layout exactly for a get_frame() dict,
# which is what makes get_frame('....h5') able to read the result.
_DATASET_KEYS = ("image", "xx", "yy")
_ATTR_MAX_SIZE = 64


def next_ssss_stem(directory, prefix="ssss", reserve=()):
    """Return a Path stem (no extension) that doesn't clash with existing files.

    Mirrors ssss.m: a custom prefix tries the bare name first, then appends
    _2, _3, ...  The default 'ssss' prefix is always numbered (ssss_001,
    ssss_002, ...).  A stem is considered taken if ANY of _EXTS exists.

    reserve: extensions to atomically create as empty placeholder files
        before returning, so a concurrent saver scanning at the same moment
        can't pick the same stem and silently overwrite the result.  The
        caller owns those files from then on and must remove them if the
        save subsequently fails (they are empty, and would otherwise burn
        a number).  Default () = scan only, no reservation.
    """
    directory = Path(directory)

    def _free(stem):
        return all(not stem.with_suffix(e).exists() for e in _EXTS)

    def _claim(stem):
        """Exclusively create each reserved extension; True if we got them all."""
        claimed = []
        for e in reserve:
            path = stem.with_suffix(e)
            try:
                path.open("xb").close()      # x = fail if it already exists
            except FileExistsError:
                for c in claimed:            # lost the race -- undo
                    c.unlink()
                return False
            claimed.append(path)
        return True

    def _candidates():
        if prefix != "ssss":
            yield directory / prefix         # custom prefix: bare name first
            n = 2
            while True:
                yield directory / f"{prefix}_{n}"
                n += 1
        else:
            n = 1
            while True:
                yield directory / f"ssss_{n:03d}"
                n += 1

    for stem in _candidates():
        if _free(stem) and _claim(stem):
            return stem


def _write_h5(path, data):
    """Write a data dict to HDF5 in beamview's snapshot layout.

    image/xx/yy become compressed datasets, as do any other large arrays;
    everything else becomes a file attribute.  None values are skipped --
    HDF5 attributes can't hold None (beamview omits unique_id the same way
    when a camera doesn't serve one).
    """
    import h5py

    with h5py.File(path, "w") as f:
        for key, value in data.items():
            if value is None:
                continue
            arr = np.asarray(value)
            big = arr.ndim > 0 and arr.size > _ATTR_MAX_SIZE
            if key in _DATASET_KEYS or (big and arr.dtype.kind not in "US"):
                f.create_dataset(key, data=arr, compression="gzip",
                                 compression_opts=4)
            else:
                f.attrs[key] = value


def ssss(fig=None, name="ssss", data=None, directory=None, dpi=150):
    """Save a figure (and optionally its data) into today's data directory.

    fig: a matplotlib Figure or Axes, or None (default) for the current
        figure.  Axes is accepted because plot_frame() returns one, so
        `bbl.ssss(bbl.plot_frame(frame))` works.
    name: filename prefix.  The default 'ssss' is always numbered
        (ssss_001, ssss_002, ...); any other prefix is used bare the first
        time, then suffixed _2, _3, ...
    data: optional dict written alongside as <stem>.h5.  Pass a get_frame()
        dict to get a file get_frame() can read back.
    directory: override the destination (default: today's data directory).
    dpi: PNG resolution.

    Returns the Path of the PNG written.
    """
    import matplotlib.pyplot as plt

    if fig is None:
        fig = plt.gcf()
    if not hasattr(fig, "savefig"):          # an Axes (or anything with .figure)
        fig = getattr(fig, "figure", None)
    if not hasattr(fig, "savefig"):
        raise TypeError("fig must be a matplotlib Figure or Axes (or None "
                        "for the current figure)")

    directory = Path(directory) if directory is not None else get_todays_directory()
    if not directory.exists():
        raise FileNotFoundError(
            f"data directory not found: {directory}  (a cron job creates it "
            f"on-site; pass directory= to save somewhere else)")

    exts = (".png", ".h5") if data is not None else (".png",)
    stem = next_ssss_stem(directory, prefix=name, reserve=exts)

    try:
        png_path = stem.with_suffix(".png")
        fig.savefig(png_path, dpi=dpi)
        if data is not None:
            _write_h5(stem.with_suffix(".h5"), data)
    except BaseException:
        # Don't leave the reserved placeholders behind burning a number
        for e in exts:
            p = stem.with_suffix(e)
            if p.exists() and p.stat().st_size == 0:
                p.unlink()
        raise

    written = " + ".join(stem.with_suffix(e).name for e in exts)
    print(f"[ssss] saved {written}  in {directory}")
    return png_path
