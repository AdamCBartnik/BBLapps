r"""
screen_sensitivity_correction() -- divide out a viewscreen's non-uniform
response using a max-hold sensitivity map.

Make the map in beamview: tick "Save max value", sweep the beam over the
screen so every pixel gets illuminated at some point, and save the result.
Each pixel then holds the brightest value it ever saw, which is a proxy
for how efficiently that part of the screen converts beam to light.

    import BBL as bbl
    sens  = bbl.get_frame('screen_sensitivity.h5')
    frame = bbl.get_frame('beam.h5')
    fixed = bbl.screen_sensitivity_correction(frame, sens)
    bbl.plot_frame(fixed)

Two things make this less trivial than frame / sensitivity:

* The map is only meaningful where the sweep actually WENT. Elsewhere it
  holds the dark background, and dividing by that amplifies noise
  enormously (on the first real map: background ~112 counts against ~800
  inside, so an 8x blow-up). Those pixels are masked out instead, and the
  map's own histogram is bimodal enough to find the boundary
  automatically.

* A maximum over frames is noisy pixel-to-pixel, and dividing by a noisy
  map injects that noise into the result. A light blur fixes it -- but
  only a light one. Measured against the first real pair, by how much of
  the map's structure survives in the corrected image:

      no correction  0.65        sigma 2   0.30
      sigma 0       -0.14        sigma 4   0.45
      sigma 1        0.11 (best) sigma 8   0.58

  Past about sigma 2 the blurred map stops matching the sharp edges of
  the real defects and the correction quietly stops working -- while
  LOOKING better, because it's smoother. Hence the low default, and why
  raising it is usually the wrong instinct.

CAVEATS, if you want this to be quantitative rather than cosmetic:
  * A max is biased by dwell time. Pixels the beam linned on, or crossed
    near its peak, read higher than ones it merely clipped, so part of the
    gain variation is sweep coverage rather than screen response. Sweeping
    at a steady speed helps; accumulating a sum rather than a max would
    remove the dependence entirely.
  * It assumes both images share a zero level. They don't exactly: a max
    over noise is biased upward, so a map's background sits above a single
    frame's. Subtracting a dark frame from each before correcting is the
    fix where it matters.
"""
import numpy as np


def _otsu_threshold(x, nbins=256):
    """Split a bimodal distribution -- here, unswept background versus
    swept screen. Otsu's method: pick the cut maximising between-class
    variance. No scikit-image dependency for ten lines of numpy."""
    hist, edges = np.histogram(x, bins=nbins)
    centres = 0.5 * (edges[1:] + edges[:-1])
    w0 = np.cumsum(hist)
    w1 = w0[-1] - w0
    csum = np.cumsum(hist * centres)
    total = csum[-1]
    with np.errstate(invalid="ignore", divide="ignore"):
        mu0 = csum / w0
        mu1 = (total - csum) / w1
        between = w0 * w1 * (mu0 - mu1) ** 2
    return float(centres[int(np.nanargmax(between))])


def _as_image(x, what):
    if isinstance(x, dict):
        return np.asarray(x["image"], dtype=np.float64)
    a = np.asarray(x, dtype=np.float64)
    if a.ndim != 2:
        raise ValueError(f"{what} must be a 2-D image or a get_frame dict, "
                         f"got shape {a.shape}")
    return a


def screen_sensitivity_correction(frame, sensitivity, sigma=1.0,
                                  threshold=None, floor=0.25, erode=9,
                                  fill=np.nan, return_details=False):
    """Correct `frame` for the screen response recorded in `sensitivity`.

    frame, sensitivity: get_frame() dicts (live, or loaded from .h5) or
        plain 2-D arrays. They must be the same shape -- i.e. the same
        camera and the same hardware ROI.
    sigma: gaussian blur applied to the map before dividing, in pixels.
        Suppresses the max's pixel-to-pixel noise. Keep it small; see the
        module docstring for why bigger looks better and works worse.
    threshold: sensitivity value separating swept from unswept. Default
        None picks it automatically from the map's histogram (Otsu).
    floor: smallest gain permitted, as a fraction of the median gain --
        caps how much a low-sensitivity pixel can be amplified.
    erode: shrink the valid region by this many pixels to drop the
        partially-swept rim, whose value isn't a sensitivity. 0 disables.
    fill: value written where the map has no coverage. NaN (default)
        makes "not measured" obvious and propagates honestly; 0 or
        np.nan_to_num afterwards if you need a plain array.
    return_details: also return the mask and gain used.

    Returns a corrected copy of `frame` -- a dict if `frame` was one (so
    plot_frame/ssss work on it directly), otherwise an array. With
    return_details=True, returns (corrected, info_dict).
    """
    from scipy.ndimage import (gaussian_filter, binary_erosion,
                               binary_fill_holes)

    img = _as_image(frame, "frame")
    smap = _as_image(sensitivity, "sensitivity")
    if img.shape != smap.shape:
        raise ValueError(
            f"frame {img.shape} and sensitivity {smap.shape} differ -- they "
            f"must come from the same camera with the same hardware ROI")

    thr = _otsu_threshold(smap) if threshold is None else float(threshold)

    # Where the sweep actually went. Fill pinholes left by dead pixels, then
    # erode so the soft rim of the swept area -- only partly illuminated, so
    # its value understates the true sensitivity -- doesn't over-amplify.
    valid = binary_fill_holes(smap > thr)
    if erode and erode > 1:
        valid = binary_erosion(valid, np.ones((int(erode), int(erode))))
    if not valid.any():
        raise ValueError(
            f"no valid region: nothing in the sensitivity map exceeds "
            f"{thr:.4g}. Wrong map, or the sweep never illuminated anything?")

    blurred = gaussian_filter(smap, sigma) if sigma > 0 else smap
    gain = blurred / np.median(blurred[valid])
    gain = np.clip(gain, floor, None)

    out = np.full(img.shape, fill, dtype=np.float64)
    out[valid] = img[valid] / gain[valid]

    if isinstance(frame, dict):
        corrected = dict(frame)
        corrected["image"] = out
        corrected["display_min"] = 0.0
        corrected["display_max"] = float(np.nanpercentile(out, 99.9))
        for k in ("title", "camera_name"):
            if k in corrected:
                corrected[k] = f"{corrected[k]} [screen-corrected]"
        corrected["flatfield_sigma"] = float(sigma)
        corrected["flatfield_threshold"] = thr
        corrected["flatfield_floor"] = float(floor)
        corrected["flatfield_valid_fraction"] = float(valid.mean())
    else:
        corrected = out

    if return_details:
        return corrected, {"valid": valid, "gain": gain, "threshold": thr,
                           "valid_fraction": float(valid.mean()),
                           "gain_min": float(gain[valid].min()),
                           "gain_max": float(gain[valid].max())}
    return corrected
