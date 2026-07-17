"""
Python port of matlab_code/utilities/measure_trend/measure_trend.m,
simplified: scan one command PV over a list of setpoints, measure the
average/std of monitor PVs at each point, live-plot as the scan runs,
and fit a polynomial to each trend at the end.

Meant for JupyterLab with `%matplotlib widget` (ipympl) so the plots
update live during the scan.
"""
import time

import numpy as np

from .live_plot import LivePlot
from .pv_tools import _sample, caput, restore_pvs
from .fitting import polyfit_weights


def measure_trend(cmd_pv, setpoints, monitor_pvs, n_avg=15, cmd_pause=0.0,
                  pause=0.0, max_pause=5.0, poly_deg=1, plot=True,
                  fresh=True):
    """Scan cmd_pv over setpoints and measure the trend of monitor_pvs.

    At each setpoint: write cmd_pv (confirmed, caput wait=True), wait
    `cmd_pause` seconds to settle, then average n_avg reads of each
    monitor PV.  Each read waits at least `pause` seconds and for a
    fresh camonitor update that arrives AFTER the read started, up to
    `max_pause` (bbl.caget semantics with fresh=True — the veto applies
    even when n_avg=1).  That fresh-update wait vetoes
    whatever value is already sitting in the monitor cache — the labca
    veto_current_data / wait_until_new_data pattern — so the first read
    after a command change can never be a stale frame, and the defaults
    (cmd_pause=0, pause=0) simply pace the scan by new data arriving.
    If a monitor PV stops updating for max_pause seconds, that scan
    point comes back NaN (caget bails out rather than average stale
    data); the plot shows a gap there and the final fit skips it.
    That veto assumes monitors that update continuously (beamview
    stats).  For monitor PVs that only post on CHANGE (e.g. a settled
    readback), pass fresh=False to sample the cached values instead.
    cmd_pv is restored to its initial value at the end, including on
    Ctrl-C / kernel interrupt.

    monitor_pvs may be a single name or a sequence.
    poly_deg is the degree of the final weighted fit (None = no fit).

    Returns a dict with setpoints, avg / std arrays of shape
    (n_points, n_monitors), fits {monitor_pv: (coeffs, coeff_errs)},
    and the LivePlot objects.
    """
    names = [monitor_pvs] if isinstance(monitor_pvs, str) else list(monitor_pvs)
    setpoints = np.asarray(setpoints, dtype=float)
    n_pts = len(setpoints)

    avg = np.full((n_pts, len(names)), np.nan)
    std = np.full((n_pts, len(names)), np.nan)

    live_plots = []
    if plot:
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(len(names), 1, sharex=True, squeeze=False,
                                 figsize=(6.0, 3.0 * len(names)))
        for name, ax in zip(names, axes[:, 0]):
            live_plots.append(LivePlot(ylabel=name, ax=ax))
        axes[-1, 0].set_xlabel(cmd_pv)
        fig.tight_layout()

    with restore_pvs(cmd_pv):
        for i, sp in enumerate(setpoints):
            print(f"[{i + 1}/{n_pts}] {cmd_pv} = {sp:g}")
            caput(cmd_pv, sp)
            time.sleep(cmd_pause)
            avg[i], std[i] = _sample(names, n_avg=n_avg, pause=pause,
                                     max_pause=max_pause, fresh=fresh)
            for k, lp in enumerate(live_plots):
                lp.update(setpoints[:i + 1], avg[:i + 1, k],
                          y_err=std[:i + 1, k])

    fits = {}
    if poly_deg is not None:
        for k, name in enumerate(names):
            if live_plots:
                fits[name] = live_plots[k].fit(deg=poly_deg)
            else:
                coeffs, errs, _ = polyfit_weights(setpoints, avg[:, k],
                                                  std[:, k], poly_deg)
                fits[name] = (coeffs, errs)

    return dict(cmd_pv=cmd_pv, setpoints=setpoints, monitor_pvs=names,
                avg=avg, std=std, fits=fits, live_plots=live_plots)
