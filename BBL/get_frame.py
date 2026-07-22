"""
get_frame() — grab the current frame + metadata from a camera IOC over
EPICS.  plot_frame() — matplotlib-plot the result, or an .h5 file saved
by beamview's "Make New Figure" -> Save feature.

Both return/accept the same dict shape, so a live get_frame() and a
saved .h5 (via load_h5_frame) are interchangeable:

    image, xx, yy                                       -- the frame
    title, camera_name, exposure_ms, gain,
    colormap, cmap_reversed, display_min, display_max   -- camera and/or beamview settings
    bits, width, height, roi, unique_id, timestamp      -- extras, get_frame() only

Usage:
    import BBL as bbl
    frame = bbl.get_frame('B24Screen1') 
    bbl.plot_frame(frame)

    frame2 = bbl.load_h5_frame('ssss_001.h5')
    bbl.plot_frame(frame2, log=True)
"""
import time
import numpy as np

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


def get_frame(camera_name, units="physical", timeout=5.0):
    """Grab the current frame from a camera IOC's areaDetector PVs.

    camera_name: the camera's own EPICS areaDetector prefix (as used by
        beamview's config), e.g. 'B24Screen1' -- NOT beamview's
        "To EPICS" publish prefix (a different PV namespace; see
        BBL.solenoid_scan's `screen` parameter for that one).
    units: 'physical' (default) -- xx/yy in the camera's calibrated
        unit via cam1:CalibX/_Y, falling back to pixels with a printed
        note if the camera isn't calibrated -- or 'pixels'.

    Returns a dict in the same shape as load_h5_frame() (see module
    docstring), so both work with plot_frame().  Raises RuntimeError if
    no frame is available (zero-size image) or the image read fails.
    """
    import epics

    p = camera_name.rstrip(":")

    def get(suffix, **kw):
        return epics.caget(f"{p}:{suffix}", timeout=timeout, **kw)

    w = int(get("image1:ArraySize0_RBV") or 0)
    h = int(get("image1:ArraySize1_RBV") or 0)
    if w <= 0 or h <= 0:
        raise RuntimeError(f"{camera_name}: no active frame "
                           f"(image1:ArraySize0/1_RBV = {w}x{h})")

    raw = epics.caget(f"{p}:image1:ArrayData", count=w * h,
                      timeout=timeout, as_numpy=True)
    if raw is None:
        raise RuntimeError(f"{camera_name}: image1:ArrayData read failed")
    image = np.asarray(raw)[:w * h].reshape(h, w)

    wmax = int(get("cam1:MaxSizeX_RBV") or w)
    hmax = int(get("cam1:MaxSizeY_RBV") or h)
    rx = int(get("cam1:MinX_RBV") or 0)
    ry = int(get("cam1:MinY_RBV") or 0)

    bits = get("cam1:BitsPerPixel_RBV")
    if bits is None:
        dt = get("cam1:DataType_RBV", as_string=True)
        bits = _DATATYPE_BITS.get(dt, 16)
    bits = int(bits)

    exposure_s = get("cam1:AcquireTime_RBV")
    exposure_ms = float(exposure_s) * 1000.0 if exposure_s is not None else 0.0
    gain = float(get("cam1:Gain_RBV") or 1.0)
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
        colormap="Gray", cmap_reversed=False,
        display_min=0.0, display_max=float(2 ** bits - 1),
        bits=bits, width=w, height=h,
        roi=(rx, ry, w, h), unique_id=uid, timestamp=ts,
    )


def load_h5_frame(path):
    """Load a beamview 'ssss' snapshot .h5 file into the same dict shape
    get_frame() returns (image/xx/yy datasets + whatever attrs the file
    has), so both are interchangeable inputs to plot_frame()."""
    import h5py

    with h5py.File(path, "r") as f:
        data = dict(image=f["image"][()], xx=f["xx"][()], yy=f["yy"][()])
        data.update(dict(f.attrs))
    return data


def plot_frame(data, ax=None, log=False, show_colorbar=True, cmap=None,
               vmin=None, vmax=None, title=None):
    """Plot a frame from get_frame() or load_h5_frame() with matplotlib.

    log: display log10(1 + |image|) instead of the raw values.  The
        stored display_min/max are for the RAW image, so with log=True
        (and vmin/vmax not given) the color range is auto-scaled to the
        transformed data instead of using the (now inconsistent) stored
        range.
    cmap: override the colormap name (else data['colormap'], via
        BBL.get_colormap; falls back to matplotlib gray on any failure).
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

    if ax is None:
        _, ax = plt.subplots()
    fig = ax.figure

    dx = xx[1] - xx[0] if len(xx) > 1 else 1.0
    dy = abs(yy[0] - yy[1]) if len(yy) > 1 else 1.0
    extent = (xx[0] - 0.5 * dx, xx[-1] + 0.5 * dx,
             yy[-1] - 0.5 * dy, yy[0] + 0.5 * dy)

    cmap_name = cmap or data.get("colormap", "Gray")
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

    im = ax.imshow(img, extent=extent, origin="upper", cmap=mpl_cmap,
                   vmin=vmin, vmax=vmax, aspect="equal")
    ax.set_title(title if title is not None else data.get("title", ""),
                fontsize=10)
    if show_colorbar:
        fig.colorbar(im, ax=ax)
    return ax
