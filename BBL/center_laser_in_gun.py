"""
Find the electrical center of the gun — port of center_laser_in_gun.m.

Raster-scan the laser spot across the cathode with the two laser stages.
At each grid point, steer the beam back to its starting spot on the
viewscreen with the gun correctors (closed loop); the corrector current
needed to undo the gun's kick, plus the residual screen offset, measures
the beam deflection.  Fit a cubic-aberration-plus-rotation model; the
cathode position where the aberration vanishes is the electrical center.

Differences from the MATLAB version (by design):
  - results are REPORTED (and returned), never written to _save PVs
  - every touched command PV — stages, correctors, solenoid — is
    restored on exit, including Ctrl-C (restore_pvs)
  - the recenter / move / settle loops all have timeouts
  - live scatter plot of the measured beam positions while scanning

Pre-flight checklist (from the original README):
  200 um pinhole in; ND >= 1.0; pockels cell CW; beam well centered and
  visible on the gun viewscreen in beamview; background subtracted;
  threshold ~50%; first solenoid will be zeroed for you.

Usage:
    pvs = dict(
        laser_h_cmd='...', laser_h_rdbk='...',
        laser_v_cmd='...', laser_v_rdbk='...',
        corr_h_cmd='...',  corr_h_rdbk='...',
        corr_v_cmd='...',  corr_v_rdbk='...',
        sol_cmd='...',
        centroid_x='...',  centroid_y='...',   # beamview published, mm
        gun_volt='...',                        # kV readback (optional)
    )
    data = bbl.center_laser_in_gun(pvs, calib_h=-0.05, calib_v=0.06)
    ...
    bbl.fit_gun_aberration(data)      # refit later without rescanning
"""
import math
import time

import numpy as np

from .pv_tools import caget, caput, restore_pvs
from .live_plot import LivePlot, display_canvas, set_plot_interactive

_PV_KEYS = ("laser_h_cmd", "laser_h_rdbk", "laser_v_cmd", "laser_v_rdbk",
            "corr_h_cmd", "corr_h_rdbk", "corr_v_cmd", "corr_v_rdbk",
            "sol_cmd", "centroid_x", "centroid_y")

_MC2_KV = 511.0   # electron rest mass, in kV units (voltages are kV)

_PARAM_NAMES = ("ax", "ay", "bx", "by", "x_off", "y_off",
                "xc", "yc", "theta")


def _momentum(volt_kv):
    return math.sqrt((volt_kv + _MC2_KV) ** 2 - _MC2_KV ** 2)


def _aberration_model(beta, laser_xy):
    """Cubic aberration + rotation; beta per _PARAM_NAMES, xy in mm."""
    ax, ay, bx, by, x_off, y_off, xc, yc, th = beta
    x = laser_xy[:, 0] - xc
    y = laser_xy[:, 1] - yc
    r2 = x * x + y * y
    xm = ax * x + bx * r2 * x
    ym = ay * y + by * r2 * y
    c, s = math.cos(th), math.sin(th)
    return np.column_stack([x_off + c * xm + s * ym,
                            y_off - s * xm + c * ym])


def fit_gun_aberration(data, beta0=None, verbose=True):
    """Fit the cubic-aberration model to a center_laser_in_gun scan.

    data: the dict returned by center_laser_in_gun (needs 'laser_pos'
    and 'beam_pos'), so old scans can be refit without rescanning.

    Returns a dict with params {name: value}, errs {name: error}, cov,
    and model_pos (fitted beam positions).  Errors are the standard
    residual-scaled least-squares estimates (like MATLAB nlinfit).
    """
    from scipy.optimize import least_squares

    X = np.asarray(data["laser_pos"], dtype=float)
    B = np.asarray(data["beam_pos"], dtype=float)

    if beta0 is None:
        # center the guess on the scanned grid; small nonzero cubic terms
        # break the xc/x_off degeneracy of the purely linear model
        beta0 = [1.05, 1.05, 0.02, 0.02,
                 float(np.mean(B[:, 0])), float(np.mean(B[:, 1])),
                 float(np.mean(X[:, 0])), float(np.mean(X[:, 1])), 0.0]

    res = least_squares(
        lambda b: (_aberration_model(b, X) - B).ravel(), beta0)

    dof = res.fun.size - len(beta0)
    s2 = 2.0 * res.cost / max(dof, 1)
    try:
        cov = np.linalg.inv(res.jac.T @ res.jac) * s2
        errs_arr = np.sqrt(np.diag(cov))
    except np.linalg.LinAlgError:
        cov = np.full((len(beta0), len(beta0)), np.nan)
        errs_arr = np.full(len(beta0), np.nan)

    params = dict(zip(_PARAM_NAMES, res.x))
    errs = dict(zip(_PARAM_NAMES, errs_arr))

    if verbose:
        print("-" * 60)
        print(f"Electrical center:  horz = {params['xc']:.3f} "
              f"± {errs['xc']:.3f} mm,  vert = {params['yc']:.3f} "
              f"± {errs['yc']:.3f} mm")
        print(f"(rotation {math.degrees(params['theta']):+.2f} deg, "
              f"a = ({params['ax']:.3f}, {params['ay']:.3f}), "
              f"b = ({params['bx']:.4f}, {params['by']:.4f}) mm^-2)")
        print("-" * 60)

    return dict(params=params, errs=errs, cov=cov,
                model_pos=_aberration_model(res.x, X),
                success=res.success)


def _wait_for(read_fn, tolerance, timeout, what):
    """Poll read_fn() until |value| <= tolerance; warn on timeout."""
    t0 = time.monotonic()
    while abs(read_fn()) > tolerance:
        if time.monotonic() - t0 > timeout:
            print(f"[center] WARNING: {what} not settled after "
                  f"{timeout:g} s (off by {read_fn():+.4g})")
            return False
        time.sleep(0.05)
    return True


def center_laser_in_gun(pvs, scan_range=7.0, num_points=11, n_avg=2,
                        calib_h=-0.044, calib_v=0.056, calib_kv=350.0,
                        position_accuracy=0.02, magnet_tolerance=0.005,
                        max_recenter_iter=20, move_timeout=60.0,
                        settle_timeout=10.0, sol_pause=3.0, max_pause=5.0,
                        plot=True, verbose=True):
    """Measure the gun electrical center.  Returns a data dict; refit
    any time with fit_gun_aberration(data).

    pvs: dict of PV names — keys laser_h_cmd/_rdbk, laser_v_cmd/_rdbk,
         corr_h_cmd/_rdbk, corr_v_cmd/_rdbk, sol_cmd, centroid_x/_y
         (beamview published, mm), and optionally gun_volt (kV readback,
         used to momentum-scale the corrector calibration).
    scan_range:  full scan span in both axes, mm on the cathode
    num_points:  grid size per axis (num_points^2 total)
    n_avg:       centroid frames averaged per reading (stale-vetoed)
    calib_h/v:   corrector calibration, A per mm ON SCREEN, measured at
                 calib_kv.  Signs matter — a wrong sign makes the
                 recentering loop diverge (it aborts after
                 max_recenter_iter).
    position_accuracy: stage tolerance and recenter tolerance, mm
    """
    missing = [k for k in _PV_KEYS if k not in pvs]
    if missing:
        raise ValueError(f"pvs dict missing keys: {', '.join(missing)}")

    # momentum-scale the corrector calibration from calib_kv to the
    # actual gun voltage (calibration is deflection per unit current,
    # which scales with beam rigidity)
    gun_kv = None
    scale = 1.0
    if pvs.get("gun_volt"):
        gun_kv = caget(pvs["gun_volt"])
        if np.isfinite(gun_kv):
            scale = _momentum(gun_kv) / _momentum(calib_kv)
        else:
            print("[center] WARNING: gun voltage unreadable; "
                  "using calibration unscaled")
    cal_h = calib_h * scale
    cal_v = calib_v * scale

    cx_pv, cy_pv = pvs["centroid_x"], pvs["centroid_y"]

    def read_centroid():
        avg, _ = caget([cx_pv, cy_pv], n_avg=n_avg, stale=True,
                       max_pause=max_pause, return_std=True)
        if not np.all(np.isfinite(avg)):
            raise RuntimeError("centroid read returned NaN — beam lost, "
                               "or beamview stopped publishing?")
        return avg

    def move_laser(axis, target):
        cmd, rdbk = pvs[f"laser_{axis}_cmd"], pvs[f"laser_{axis}_rdbk"]
        caput(cmd, target)
        ok = _wait_for(lambda: caget(rdbk) - target, position_accuracy,
                       move_timeout, f"laser {axis} -> {target:.3f} mm")
        if not ok:
            raise RuntimeError(f"laser stage {axis} did not reach "
                               f"{target:.3f} mm within {move_timeout:g} s")

    def set_correctors(i_h, i_v):
        caput([pvs["corr_h_cmd"], pvs["corr_v_cmd"]], [i_h, i_v])
        _wait_for(lambda: caget(pvs["corr_h_rdbk"]) - i_h,
                  magnet_tolerance, settle_timeout, "corrector H")
        _wait_for(lambda: caget(pvs["corr_v_rdbk"]) - i_v,
                  magnet_tolerance, settle_timeout, "corrector V")

    def recenter(target):
        """Steer the beam back to `target` on the screen; returns the
        final corrector currents (from the command PVs)."""
        for _ in range(max_recenter_iter):
            c = read_centroid()
            ex, ey = c[0] - target[0], c[1] - target[1]
            if abs(ex) < position_accuracy and abs(ey) < position_accuracy:
                return (float(caget(pvs["corr_h_cmd"])),
                        float(caget(pvs["corr_v_cmd"])))
            set_correctors(float(caget(pvs["corr_h_cmd"])) - ex * cal_h,
                           float(caget(pvs["corr_v_cmd"])) - ey * cal_v)
        raise RuntimeError(
            f"recentering did not converge in {max_recenter_iter} "
            f"iterations (last error ({ex:+.3f}, {ey:+.3f}) mm) — beam "
            "lost, or wrong calibration sign?")

    # ---- initial state ----------------------------------------------------
    h0 = float(caget(pvs["laser_h_rdbk"]))
    v0 = float(caget(pvs["laser_v_rdbk"]))
    i_h0 = float(caget(pvs["corr_h_cmd"]))
    i_v0 = float(caget(pvs["corr_v_cmd"]))
    if verbose:
        print(f"Initial stages: ({h0:.3f}, {v0:.3f}) mm; "
              f"correctors: ({i_h0:+.3f}, {i_v0:+.3f}) A")
        if gun_kv is not None:
            print(f"Gun at {gun_kv:.0f} kV -> calibration x{scale:.3f}: "
                  f"({cal_h:+.4f}, {cal_v:+.4f}) A/mm")

    step = scan_range / (num_points - 1)
    rel = np.linspace(-scan_range / 2, scan_range / 2, num_points)

    laser_pos = []
    beam_pos = []

    lp = None
    if plot:
        lp = LivePlot(xlabel="beam x (rel. mm)", ylabel="beam y (rel. mm)",
                      title="gun centering scan", style="bo")
        lp.ax.set_aspect("equal", adjustable="datalim")
        set_plot_interactive(lp.fig, False)

    try:
        with restore_pvs(pvs["laser_h_cmd"], pvs["laser_v_cmd"],
                         pvs["corr_h_cmd"], pvs["corr_v_cmd"],
                         pvs["sol_cmd"]):
            # solenoid off — its rotation would corrupt the measurement
            caput(pvs["sol_cmd"], 0.0)
            time.sleep(sol_pause)

            target = read_centroid()   # the spot everything returns to

            # walk to the first corner one step at a time, recentering as
            # we go, so the beam never leaves the screen
            n_walk = (num_points - 1) // 2
            for k in range(1, n_walk + 1):
                move_laser("h", h0 - k * step)
                move_laser("v", v0 - k * step)
                recenter(target)

            for ix, rx in enumerate(rel):
                if verbose:
                    print(f"scanning row {ix + 1} / {num_points}")
                # zig-zag: alternate the vertical direction each row
                col = rel if ix % 2 == 0 else rel[::-1]
                for ry in col:
                    move_laser("h", h0 + rx)
                    move_laser("v", v0 + ry)
                    i_h, i_v = recenter(target)
                    c = read_centroid()
                    bx = (c[0] - target[0]) + (i_h - i_h0) / cal_h
                    by = (c[1] - target[1]) + (i_v - i_v0) / cal_v
                    laser_pos.append([h0 + rx, v0 + ry])
                    beam_pos.append([bx, by])
                    if lp is not None:
                        arr = np.asarray(beam_pos)
                        lp.update(arr[:, 0], arr[:, 1], label="measured")
    finally:
        if lp is not None:
            set_plot_interactive(lp.fig, True)

    data = dict(laser_pos=np.asarray(laser_pos),
                beam_pos=np.asarray(beam_pos),
                target_centroid=np.asarray(target),
                initial_stage=(h0, v0), initial_corr=(i_h0, i_v0),
                calib=(cal_h, cal_v), gun_kv=gun_kv,
                scan_range=scan_range, num_points=num_points,
                live_plot=lp)

    # ---- fit + report -------------------------------------------------------
    fit = fit_gun_aberration(data, verbose=verbose)
    data["fit"] = fit
    if lp is not None:
        m = fit["model_pos"]
        lp.update(m[:, 0], m[:, 1], label="fit", style="r.")
        lp.ax.set_title(
            f"center: ({fit['params']['xc']:.3f} ± {fit['errs']['xc']:.3f}, "
            f"{fit['params']['yc']:.3f} ± {fit['errs']['yc']:.3f}) mm",
            fontsize=10)
        lp.refresh()

    return data
