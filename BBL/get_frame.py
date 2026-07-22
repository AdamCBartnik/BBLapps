"""
get_frame() — grab the current frame + metadata from a camera IOC over
EPICS, OR load a beamview 'ssss' snapshot .h5 file -- same function,
dispatched on whether the name passed in ends with '.h5'.
plot_frame() -- matplotlib-plot the result of either.

A live grab and a beamview snapshot .h5 carry the SAME fields (beamview
writes the extras too, as of the same change that added them here):

    image, xx, yy                                       -- the frame
    title, camera_name, exposure_ms, gain,
    colormap, cmap_reversed, display_min, display_max
    bits, width, height, roi, unique_id, timestamp       -- "extras"

Caveat: .h5 files saved by an OLDER beamview won't have the extras
(and a camera with no UniqueId omits unique_id even live).  plot_frame()
only needs image/xx/yy/colormap/display range, which every path always
has, so it works regardless.

Usage:
    import BBL as bbl
    frame = bbl.get_frame('B24Screen1') 
    bbl.plot_frame(frame)

    frame2 = bbl.get_frame('ssss_001.h5')     # loads the file instead
    bbl.plot_frame(frame2, log=True)
"""
import time
import numpy as np

from .pv_tools import caget
from .live_plot import display_canvas

# Bits carried by each areaDetector DataType, used when the IOC doesn't
# serve the BitsPerPixel_RBV extension (matches
# beamview/cameras/epics_areadetector.py's fallback table; duplicated
# rather than imported so BBL stays decoupled from beamview).
_DATATYPE_BITS = {
    "Int8": 8, "UInt8": 8,
    "Int16": 16, "UInt16": 16,
    "Int32": 32, "UInt32": 32,
    "Int64": 64, "UInt64": 64,
    "Float32": 32, "Float64": 64,
}

# Cached, auto-monitored PVs for the (large) image waveform only -- caget()
# above is scalar-oriented (nanmean/nanstd over samples), not appropriate
# for an array value, so the image gets its own small cache here, read
# through the monitor (use_monitor=True): a plain, un-cached
# epics.caget()/PV.get(use_monitor=False) issues a FRESH Channel Access get
# every call and waits for the IOC to service it -- for a multi-megapixel
# waveform that round trip is slow, and on some IOCs/drivers a paused
# camera's non-monitor get path is routed through the driver rather than
# just handing back the last written buffer, making it slower still. A
# monitor delivers the CURRENT value immediately on subscription (per CA
# protocol) and every call after the first is a local cache read -- fast
# and current regardless of whether the camera is actively acquiring.
_array_pvs = {}


def _get_array_pv(pvname):
    import epics
    pv = _array_pvs.get(pvname)
    if pv is None:
        pv = epics.PV(pvname, auto_monitor=True)
        _array_pvs[pvname] = pv
    return pv


def get_frame(name, units="physical", timeout=5.0):
    """Grab the current frame from a camera IOC, or load a saved .h5.

    name: either a camera's own EPICS areaDetector prefix (as used by
        beamview's config), e.g. 'B24Screen1' -- NOT beamview's
        "To EPICS" publish prefix (a different PV namespace; see
        BBL.solenoid_scan's `screen` parameter for that one) -- or the
        path to a beamview 'ssss' snapshot .h5 file (detected by a
        '.h5' suffix), loaded instead of touching EPICS at all.
    units: 'physical' (default) -- xx/yy in the camera's calibrated
        unit via cam1:CalibX/_Y, falling back to pixels with a printed
        note if the camera isn't calibrated -- or 'pixels'.  Ignored
        for a .h5 load (xx/yy come from the file as saved).

    Returns a dict: image, xx, yy, title, camera_name, exposure_ms,
    gain, colormap, cmap_reversed, display_min, display_max, bits,
    width, height, roi, unique_id, timestamp.  A beamview snapshot .h5
    carries the same fields (older files may lack the last six; see the
    module docstring).

    Raises RuntimeError if no frame is available (zero-size image) or
    the image read fails.
    """
    if str(name).endswith(".h5"):
        return _load_h5(name)

    camera_name = name
    p = camera_name.rstrip(":")

    def get(suffix, default=None):
        # caget() never raises and returns NaN on failure, not None --
        # substitute `default` so downstream int()/comparisons don't choke
        # on NaN (int(nan) raises; nan compares truthy in `or` chains).
        v = caget(f"{p}:{suffix}")
        return v if np.isfinite(v) else default

    w = int(get("image1:ArraySize0_RBV", 0))
    h = int(get("image1:ArraySize1_RBV", 0))
    if w <= 0 or h <= 0:
        raise RuntimeError(f"{camera_name}: no active frame "
                           f"(image1:ArraySize0/1_RBV = {w}x{h})")

    array_pv = _get_array_pv(f"{p}:image1:ArrayData")
    if not array_pv.wait_for_connection(timeout=timeout):
        raise RuntimeError(f"{camera_name}: image1:ArrayData not connected")
    raw = array_pv.get(count=w * h, timeout=timeout, as_numpy=True,
                       use_monitor=True)
    if raw is None:
        raise RuntimeError(f"{camera_name}: image1:ArrayData read failed")
    image = np.asarray(raw)[:w * h].reshape(h, w)

    wmax = int(get("cam1:MaxSizeX_RBV", w))
    hmax = int(get("cam1:MaxSizeY_RBV", h))
    rx = int(get("cam1:MinX_RBV", 0))
    ry = int(get("cam1:MinY_RBV", 0))

    bits = get("cam1:BitsPerPixel_RBV")
    if bits is None:
        import epics
        dt = epics.caget(f"{p}:cam1:DataType_RBV", as_string=True,
                         timeout=timeout)
        bits = _DATATYPE_BITS.get(dt, 16)
    bits = int(bits)

    exposure_ms = get("cam1:AcquireTime_RBV", 0.0) * 1000.0
    gain = get("cam1:Gain_RBV", 1.0)
    uid = get("image1:UniqueId_RBV")

    dy1 = hmax - 1 - ry   # display-y of row 0 (top of the sensor crop)

    if units == "pixels":
        xx = rx + np.arange(w, dtype=np.float64)
        yy = dy1 - np.arange(h, dtype=np.float64)
    elif units == "physical":
        sx, sy = get("cam1:CalibX"), get("cam1:CalibY")
        if not sx or not sy or sx <= 0 or sy <= 0:
            print(f"[get_frame] {camera_name}: no calibration "
                 "(cam1:CalibX/_Y) -- falling back to pixel units")
            xx = rx + np.arange(w, dtype=np.float64)
            yy = dy1 - np.arange(h, dtype=np.float64)
        else:
            xx = (rx + np.arange(w, dtype=np.float64) - 0.5 * wmax) * sx
            yy = (dy1 - np.arange(h, dtype=np.float64) - 0.5 * hmax) * sy
    else:
        raise ValueError("units must be 'physical' or 'pixels'")

    ts = time.strftime("%Y-%m-%d  %H:%M:%S")
    return dict(
        image=image, xx=xx, yy=yy,
        title=f"{camera_name}    {ts}",
        camera_name=camera_name,
        exposure_ms=exposure_ms, gain=gain,
        colormap="freeze", cmap_reversed=False,
        # default range = [0, max pixel value], as if "set range" were
        # clicked in beamview (not [0, 2**bits-1] full scale)
        display_min=0.0, display_max=float(image.max()),
        bits=bits, width=w, height=h,
        roi=(rx, ry, w, h), unique_id=uid, timestamp=ts,
    )


def _load_h5(path):
    """Load a beamview 'ssss' snapshot .h5 file into get_frame()'s dict
    shape (image/xx/yy datasets + whatever attrs the file has)."""
    import h5py

    with h5py.File(path, "r") as f:
        data = dict(image=f["image"][()], xx=f["xx"][()], yy=f["yy"][()])
        data.update(dict(f.attrs))
    return data


def plot_frame(data, ax=None, log=False, show_colorbar=True, cmap=None,
               vmin=None, vmax=None, title=None):
    """Plot a frame from get_frame() (live grab or .h5 load) with matplotlib.

    log: display log10(1 + |image|) instead of the raw values.  The
        stored display_min/max are for the RAW image, so with log=True
        (and vmin/vmax not given) the color range is auto-scaled to the
        transformed data instead of using the (now inconsistent) stored
        range.
    cmap: override the colormap name (else data['colormap'], or 'freeze'
        if the dict has none), via BBL.get_colormap; falls back to
        matplotlib gray on any failure.
    vmin/vmax: override the display range (else data['display_min'/'max']
        when log=False, auto-scaled when log=True).

    Returns the Axes plotted into.
    """
    import matplotlib.pyplot as plt

    img = np.asarray(data["image"], dtype=np.float64)
    xx = np.asarray(data["xx"], dtype=np.float64)
    yy = np.asarray(data["yy"], dtype=np.float64)
    if log:
        img = np.log10(1.0 + np.abs(img))

    dx = xx[1] - xx[0] if len(xx) > 1 else 1.0
    dy = abs(yy[0] - yy[1]) if len(yy) > 1 else 1.0
    extent = (xx[0] - 0.5 * dx, xx[-1] + 0.5 * dx,
             yy[-1] - 0.5 * dy, yy[0] + 0.5 * dy)

    cmap_name = cmap or data.get("colormap") or "freeze"
    try:
        from .get_colormap import get_colormap
        from matplotlib.colors import ListedColormap
        name = cmap_name.lower()
        if data.get("cmap_reversed") and not name.endswith("_r"):
            name += "_r"
        mpl_cmap = ListedColormap(get_colormap(name))
    except Exception:
        mpl_cmap = "gray"

    if vmin is None:
        vmin = float(img.min()) if log else data.get("display_min", float(img.min()))
    if vmax is None:
        vmax = float(img.max()) if log else data.get("display_max", float(img.max()))

    created = ax is None
    # Build and FULLY render the figure with interactive mode off, before it
    # is ever shown to the frontend.  Populating an ipympl figure AFTER it is
    # displayed races the widget's initial image request -- the frontend can
    # latch onto a half-populated frame (colorbar/title being the last things
    # added), which showed up as an intermittently clipped colorbar and
    # missing title pixels.  Rendering first, displaying second, means the
    # widget's first (and only) frame is already complete.
    with plt.ioff():
        if created:
            _, ax = plt.subplots()
        fig = ax.figure

        im = ax.imshow(img, extent=extent, origin="upper", cmap=mpl_cmap,
                       vmin=vmin, vmax=vmax, aspect="equal")
        ax.set_title(title if title is not None else data.get("title", ""),
                    fontsize=10)
        if show_colorbar:
            fig.colorbar(im, ax=ax)
        fig.canvas.draw()   # settle layout + render into the Agg buffer

    if created:
        display_canvas(fig)   # display the already-complete figure
    else:
        # existing, already-displayed figure: force one full (non-diff)
        # frame so the added artists can't come through as a partial update
        if hasattr(fig.canvas, "_force_full"):
            fig.canvas._force_full = True
        fig.canvas.draw_idle()
        try:
            fig.canvas.flush_events()
        except Exception:
            pass
    return ax
