"""
Solenoid alignment scan — port of solenoid_scan.m + solalign_3methods.m
(the "which_method = 2" fit only; the other three were unused variants).

Ramp a solenoid's current, recording the beam centroid on a downstream
screen at each setpoint (with an automatic laser-power servo to keep the
screen usefully but not saturatingly illuminated), then fit the
resulting (x, y) trajectory against a measured on-axis field map to
recover the beam's position and angle (x, x', y, y') at the solenoid's
entrance — the standard "solenoid scan" emittance-line diagnostic.

Field maps: pass the path to a .gdf file (General Particle Tracer's
format; loaded with the `easygdf` package) containing either a 1D
on-axis map (blocks 'Z', 'Bz') or a 2D (r, z) map (blocks 'R', 'Z',
'Bz', ...) — the on-axis slice is extracted automatically.  The map
must be normalized to 1 A of solenoid excitation, uniform in z.

Usage:
    pvs = dict(
        sol_cmd='...', sol_rdbk='...',      # solenoid current, A
        screen='B24',                       # beamview publish prefix (the
                                             # area-level EPICS prefix) --
                                             # centroid_x/_y and
                                             # peak_intensity are read as
                                             # "<screen>:centroid_x" etc.
        laser_power_cmd='...',              # optional: auto-intensity servo
        camera='B24Screen1',                # required if laser_power_cmd is
                                             # given -- the CAMERA's own
                                             # areaDetector prefix (its config
                                             # id). Full scale = 2**bits from
                                             # "<camera>:cam1:BitsPerPixel_RBV"
                                             # (standard name; separate
                                             # namespace from `screen`)
        gun_volt='...',                     # optional: kV readback, for Brho
    )
    data = bbl.solenoid_scan(pvs, np.linspace(-0.5, -5.0, 15),
                             fieldmap='solenoid_R128_sg.gdf',
                             drift_length=1.234)   # solenoid CENTER to screen, m
    ...
    bbl.fit_solenoid_scan(data, 'solenoid_R128_sg.gdf', 1.234)  # refit
"""
import math
import time

import numpy as np

from .pv_tools import caget, caput, restore_pvs
from .live_plot import LivePlot, set_plot_interactive
from .physics import momentum_from_voltage_kv, brho_tesla_meters

_PARAM_NAMES = ("x_off", "xp_off", "y_off", "yp_off")


# ---------------------------------------------------------------------------
# Field map
# ---------------------------------------------------------------------------

def load_onaxis_field(gdf_path):
    """Load an on-axis Bz(z) field map (T per A) from a .gdf file.

    Handles both a 1D map (blocks 'Z', 'Bz') and a 2D (r, z) map (blocks
    'R', 'Z', 'Bz', ...), extracting the on-axis (smallest |R|) slice in
    the latter case.  Returns (z, bz), sorted by z, on a uniform grid.
    """
    import easygdf

    d = easygdf.load(str(gdf_path))
    blocks = {b["name"].strip().lower(): np.asarray(b["value"], dtype=float)
              for b in d["blocks"]}
    if "z" not in blocks or "bz" not in blocks:
        raise ValueError(f"{gdf_path}: expected GDF blocks 'Z' and 'Bz'; "
                         f"found {list(blocks)}")
    z, bz = blocks["z"], blocks["bz"]

    if "r" in blocks:
        r = blocks["r"]
        r0 = r[np.argmin(np.abs(r))]
        if not np.isclose(r0, 0.0, atol=1e-6):
            print(f"[solenoid] WARNING: field map's smallest |R| is "
                  f"{r0:g} m, not exactly 0 — using it as the on-axis "
                  "approximation")
        mask = np.isclose(r, r0, atol=1e-9)
        z, bz = z[mask], bz[mask]

    order = np.argsort(z)
    z, bz = z[order], bz[order]
    dz = np.diff(z)
    if not np.allclose(dz, dz[0], rtol=1e-3):
        raise ValueError(f"{gdf_path}: on-axis Z grid is not uniform "
                         "(required for the transfer-matrix integration)")
    return z, bz


# ---------------------------------------------------------------------------
# Transfer matrices (SI: meters, Tesla, T*m) and the fit
# ---------------------------------------------------------------------------

def _drift_matrix(length):
    return np.array([[1.0, length, 0.0, 0.0],
                     [0.0, 1.0, 0.0, 0.0],
                     [0.0, 0.0, 1.0, length],
                     [0.0, 0.0, 0.0, 1.0]])


def _solenoid_matrix(b, length, brho):
    """Coupled (rotating) 4x4 solenoid transport matrix, (x, x', y, y')."""
    if b == 0.0:
        return _drift_matrix(length)
    k = b / (2.0 * brho)
    c, s = math.cos(k * length), math.sin(k * length)
    return np.array([
        [c * c, s * c / k, s * c, s * s / k],
        [-k * s * c, c * c, -k * s * s, s * c],
        [-s * c, -s * s / k, c * c, s * c / k],
        [k * s * s, -s * c, -k * s * c, c * c],
    ])


def _field_center(z, bz):
    """Longitudinal center of the solenoid = the Bz^2-weighted centroid of
    the on-axis field (the field-energy center; exact 0 for a symmetric
    map, robust to tails, and NOT just the mid-z point).  This is the
    point the user's center-to-screen distance is measured to."""
    z = np.asarray(z, dtype=float)
    w = np.asarray(bz, dtype=float) ** 2
    return float(np.sum(z * w) / np.sum(w))


def _transfer_matrix(z, bz_per_amp, current, brho, edge_drift):
    """4x4 transport matrix, field-map start (z[0]) to the screen, at
    `current`.  edge_drift is the drift from the map's downstream edge
    (z[-1]) to the screen -- see fit_solenoid_scan for the center-to-edge
    conversion."""
    dz = z[1] - z[0]
    m = np.eye(4)
    for i in range(len(z) - 1):
        m = _solenoid_matrix(bz_per_amp[i] * current, dz, brho) @ m
    return _drift_matrix(edge_drift) @ m


def fit_solenoid_scan(data, fieldmap, drift_length, current_scale=-1.0,
                      brho=None, verbose=True):
    """Fit a solenoid_scan() result for the beam's (x, x', y, y') at the
    solenoid entrance.  data needs 'current_setpoints', 'x_avg', 'y_avg'
    (and, if brho is not given, 'momentum_kv') — so a scan can be refit
    without rescanning.  See solenoid_scan for fieldmap/drift_length/
    current_scale.

    Returns params/errs {'x_off','xp_off','y_off','yp_off'}, cov, and
    model_x/model_y (the fitted trajectory).  Offsets come out in
    whatever unit x_avg/y_avg are in (centroid_x/_y's unit); angles in
    that same unit per meter — e.g. micron and micron/m if centroid_x/_y
    are microns.  (A uniform rescaling of x_avg/y_avg is fully absorbed
    by params, so this works in any consistent screen unit — nothing
    here assumes mm.)

    drift_length (METERS) is the distance from the SOLENOID CENTER to the
    screen -- the easy thing to survey.  The center is found as the
    Bz^2-weighted centroid of the field map (_field_center, not just the
    mid-z point), and the drift the transport actually needs (from the
    map's downstream edge z[-1] to the screen) is computed internally as
    drift_length - (z[-1] - center).  (Equivalent to the MATLAB original's
    dd = distance - solpos - sollen/2, but with the true field center
    instead of assuming symmetry.)  The reported x_off/y_off is the beam
    position at the field map's UPSTREAM edge z[0].

    IMPORTANT — drift_length must be accurate: the fitted parameters
    depend strongly on it (a post-solenoid drift trades off against the
    beam angle: screen position = solenoid-exit position + L*divergence,
    so a shorter L with a larger angle traces almost the same spiral as
    a longer L with a smaller angle).  The fit QUALITY barely changes
    with L — a wrong drift_length still gives a good-looking fit — so the
    residual will NOT warn you.  It is a surveyed geometric quantity;
    supply the real value, don't tune it to the fit.
    """
    z, bz = load_onaxis_field(fieldmap)

    if brho is None:
        if data.get("momentum_kv") is None:
            raise ValueError("brho not given and data has no momentum_kv "
                            "(pass gun_volt to solenoid_scan, or pass "
                            "brho= directly)")
        brho = brho_tesla_meters(data["momentum_kv"])

    # center-to-screen (surveyed) -> downstream-edge-to-screen (transport):
    # subtract the distance from the field center to the map's exit edge
    center = _field_center(z, bz)
    edge_drift = drift_length - (z[-1] - center)
    if verbose:
        print(f"Field center at z = {center:+.4f} m; center-to-screen "
              f"{drift_length:.4f} m -> edge-to-screen drift "
              f"{edge_drift:.4f} m")

    # current_scale multiplies the setpoints before building the model:
    # its SIGN sets the solenoid's assumed field/rotation direction. The
    # default is -1.0 because this machine's solenoid polarity is swapped
    # relative to the field map's sign convention (empirically the fit is
    # far better at -1 than +1). Flip to +1.0 for a solenoid whose current
    # sign matches the map; |value| != 1 also absorbs a current-calibration
    # factor (the MATLAB original's /1.022).
    cur = np.asarray(data["current_setpoints"], dtype=float) * current_scale
    x = np.asarray(data["x_avg"], dtype=float)
    y = np.asarray(data["y_avg"], dtype=float)
    n = len(cur)

    xy = np.empty(2 * n)
    xy[0::2] = x - x[0]
    xy[1::2] = y - y[0]

    b = np.empty((2 * n, 4))
    r0 = _transfer_matrix(z, bz, cur[0], brho, edge_drift)
    for i, c in enumerate(cur):
        r = _transfer_matrix(z, bz, c, brho, edge_drift)
        b[2 * i] = r[0, :] - r0[0, :]
        b[2 * i + 1] = r[2, :] - r0[2, :]

    beta, *_ = np.linalg.lstsq(b, xy, rcond=None)
    resid = xy - b @ beta
    dof = max(len(xy) - len(beta), 1)
    sigma2 = (resid @ resid) / dof
    cov = np.linalg.inv(b.T @ b) * sigma2
    errs_arr = np.sqrt(np.diag(cov))

    params = dict(zip(_PARAM_NAMES, beta))
    errs = dict(zip(_PARAM_NAMES, errs_arr))

    model_xy = b @ beta
    model_x = model_xy[0::2] + x[0]
    model_y = model_xy[1::2] + y[0]

    if verbose:
        print("-" * 60)
        print("Beam at solenoid entrance (units: centroid_x/_y's unit; "
              "angles per meter):")
        print(f"  x  = {params['x_off']:+.4g} ± {errs['x_off']:.2g}")
        print(f"  x' = {params['xp_off']:+.4g} ± {errs['xp_off']:.2g}")
        print(f"  y  = {params['y_off']:+.4g} ± {errs['y_off']:.2g}")
        print(f"  y' = {params['yp_off']:+.4g} ± {errs['yp_off']:.2g}")
        print("-" * 60)

    return dict(params=params, errs=errs, cov=cov,
                model_x=model_x, model_y=model_y, success=True)


# ---------------------------------------------------------------------------
# The scan itself
# ---------------------------------------------------------------------------

def _wait_for(read_fn, tolerance, timeout, what):
    t0 = time.monotonic()
    while abs(read_fn()) > tolerance:
        if time.monotonic() - t0 > timeout:
            print(f"[solenoid] WARNING: {what} not settled after "
                  f"{timeout:g} s (off by {read_fn():+.4g})")
            return False
        time.sleep(0.05)
    return True


def solenoid_scan(pvs, current_setpoints, fieldmap, drift_length, n_avg=10,
                  magnet_tolerance=0.02, settle_timeout=30.0,
                  post_settle_pause=1.0, max_pause=5.0,
                  intensity_min_frac=0.10, intensity_max_frac=0.20,
                  laser_power_limit=100.0,
                  laser_power_pause=1.0, max_power_iter=20,
                  degauss=True, degauss_current=6.0, current_scale=-1.0,
                  plot=True, verbose=True):
    """Scan a solenoid, measure the beam centroid trajectory, and fit it
    (fit_solenoid_scan) for the beam's position/angle at the solenoid
    entrance.  Returns a data dict; refit any time with
    fit_solenoid_scan(data, fieldmap, drift_length).

    pvs: dict of PV names —
      sol_cmd/_rdbk:    solenoid current, A
      screen:           beamview publish prefix (the area-level EPICS
                        prefix, e.g. "B24") — centroid_x/_y and
                        peak_intensity are read as "<screen>:centroid_x".
      laser_power_cmd:  (optional) laser power setpoint; enables the
                        auto-intensity servo.  Omit to skip it.
      camera:           required if laser_power_cmd is given — the
                        camera's own areaDetector prefix (its beamview
                        config id, e.g. "B24Screen1").  The bit depth is
                        read from the standard "<camera>:cam1:
                        BitsPerPixel_RBV"; full scale for the servo is
                        2**bits.  (Separate namespace from `screen`, so it
                        can't be derived from it.)
      gun_volt:         (optional) kV readback, used with the field map
                        to compute the beam's momentum / Brho.

    current_setpoints: solenoid currents to scan, A (any order/sign —
        e.g. np.linspace(-0.5, -5.0, 15)).
    fieldmap, drift_length, current_scale: see fit_solenoid_scan.
    n_avg: centroid frames averaged per setpoint (camonitor-vetoed).

    intensity_min_frac/max_frac: target range for peak_intensity, as a
        fraction of the camera's full scale (2**bits, from
        pvs['camera']).  Only used if pvs['laser_power_cmd'] is given.
    degauss: pulse the solenoid to +/-degauss_current, then 0, before
        the scan (removes hysteresis).
    """
    required = ("sol_cmd", "sol_rdbk", "screen")
    missing = [k for k in required if k not in pvs]
    if missing:
        raise ValueError(f"pvs dict missing keys: {', '.join(missing)}")

    # screen may be given with or without a trailing colon ("B24" or
    # "B24:"), or empty for a prefix-less beamview publish -- mirror
    # beamview's own _epics_pv: "<prefix>:<name>" if prefix else "<name>".
    screen = pvs["screen"].rstrip(":")
    def _screen_pv(name):
        return f"{screen}:{name}" if screen else name
    cx_pv, cy_pv = _screen_pv("centroid_x"), _screen_pv("centroid_y")
    pk_pv = _screen_pv("peak_intensity")
    laser_pv = pvs.get("laser_power_cmd")
    intensity_full_scale = None
    if laser_pv:
        if not pvs.get("camera"):
            raise ValueError("pvs['camera'] (the camera's areaDetector "
                             "prefix) is required when "
                             "pvs['laser_power_cmd'] is given")
        bits_pv = f"{pvs['camera'].rstrip(':')}:cam1:BitsPerPixel_RBV"
        n_bits = caget(bits_pv)
        if not np.isfinite(n_bits):
            raise RuntimeError(f"{bits_pv} read returned NaN")
        intensity_full_scale = 2.0 ** n_bits
        if verbose:
            print(f"Camera bit depth: {n_bits:g} -> full scale "
                  f"{intensity_full_scale:g}")

    current_setpoints = np.asarray(current_setpoints, dtype=float)
    n_pts = len(current_setpoints)

    momentum_kv = None
    if pvs.get("gun_volt"):
        gun_kv = caget(pvs["gun_volt"])
        if np.isfinite(gun_kv):
            momentum_kv = momentum_from_voltage_kv(gun_kv)
        else:
            print("[solenoid] WARNING: gun voltage unreadable")

    def read_centroid():
        avg, std = caget([cx_pv, cy_pv], n_avg=n_avg, stale=True,
                         max_pause=max_pause, return_std=True)
        if not np.all(np.isfinite(avg)):
            raise RuntimeError("centroid read returned NaN — beam lost, "
                               "or beamview stopped publishing?")
        return avg, std

    def read_peak_intensity():
        v = caget(pk_pv, stale=True, max_pause=max_pause)
        if not np.isfinite(v):
            raise RuntimeError("peak_intensity read returned NaN")
        return v

    def adjust_laser_power():
        if laser_pv is None:
            return
        for _ in range(max_power_iter):
            frac = read_peak_intensity() / intensity_full_scale
            pw = float(caget(laser_pv))
            if frac > intensity_max_frac:
                caput(laser_pv, pw * (2.0 / 3.0))
            elif frac < intensity_min_frac:
                new_pw = 0.01 if pw < 1e-2 else pw * 1.5
                if new_pw > laser_power_limit:
                    caput(laser_pv, laser_power_limit)
                    print(f"[solenoid] WARNING: laser at power limit "
                          f"{laser_power_limit:g}, intensity fraction "
                          f"still {frac:.2f}")
                    return
                caput(laser_pv, new_pw)
            else:
                return
            time.sleep(laser_power_pause)
        print("[solenoid] WARNING: laser power servo did not converge "
              f"in {max_power_iter} iterations")

    def set_solenoid(current):
        caput(pvs["sol_cmd"], current)
        ok = _wait_for(lambda: caget(pvs["sol_rdbk"]) - current,
                       magnet_tolerance, settle_timeout,
                       f"solenoid -> {current:+.3f} A")
        if not ok:
            raise RuntimeError(f"solenoid did not reach {current:+.3f} A "
                               f"within {settle_timeout:g} s")
        time.sleep(post_settle_pause)

    x_avg = np.full(n_pts, np.nan)
    y_avg = np.full(n_pts, np.nan)
    x_std = np.full(n_pts, np.nan)
    y_std = np.full(n_pts, np.nan)

    lp = None
    if plot:
        lp = LivePlot(xlabel="beam x", ylabel="beam y",
                      title="solenoid scan (screen trajectory)", style="bo")
        lp.ax.set_aspect("equal", adjustable="datalim")
        set_plot_interactive(lp.fig, False)

    restore_targets = [pvs["sol_cmd"]] + ([laser_pv] if laser_pv else [])
    try:
        with restore_pvs(*restore_targets):
            if degauss:
                if verbose:
                    print(f"Degaussing solenoid (+/-{degauss_current:g} A)...")
                for c in (abs(degauss_current), -abs(degauss_current), 0.0):
                    set_solenoid(c)

            for i, cur in enumerate(current_setpoints):
                if verbose:
                    print(f"[{i + 1}/{n_pts}] solenoid = {cur:+.3f} A")
                set_solenoid(cur)
                adjust_laser_power()
                (cx, cy), (sx, sy) = read_centroid()
                x_avg[i], y_avg[i] = cx, cy
                x_std[i], y_std[i] = sx, sy
                if lp is not None:
                    lp.update(x_avg[:i + 1], y_avg[:i + 1],
                             y_err=y_std[:i + 1], label="measured")
    finally:
        if lp is not None:
            set_plot_interactive(lp.fig, True)

    data = dict(current_setpoints=current_setpoints,
               x_avg=x_avg, y_avg=y_avg, x_std=x_std, y_std=y_std,
               momentum_kv=momentum_kv, drift_length=drift_length,
               fieldmap=str(fieldmap), current_scale=current_scale,
               live_plot=lp)

    fit = fit_solenoid_scan(data, fieldmap, drift_length,
                            current_scale=current_scale, verbose=verbose)
    data["fit"] = fit
    if lp is not None:
        lp.update(fit["model_x"], fit["model_y"], label="fit", style="r.")
        lp.refresh()

    return data
