"""
Scan one command PV over a list of setpoints, measure the
average/std of monitor PVs at each point, live-plot as the scan runs,
and fit a polynomial to each trend at the end.

Meant for JupyterLab with `%matplotlib widget` (ipympl) so the plots
update live during the scan.
"""
import time
import matplotlib.pyplot as plt
import numpy as np

from .live_plot import LivePlot, display_canvas, set_plot_interactive
from .pv_tools import caget, caput, restore_pvs
from .fitting import polyfit_weights

def measure_trend(cmd_pv, setpoints, monitor_pvs, n_avg=15, cmd_pause=0.0,
                  pause=0.0, max_pause=5.0, poly_deg=1, plot=True,
                  stale=True, verbose=False):
    """Scan cmd_pv over setpoints and measure the trend of monitor_pvs. 
    cmd_pv is restored to its initial value at the end, including on
    Ctrl-C / kernel interrupt.
    
    cmd_pv:       The parameter to vary during the scan
    setpoints:    Values to scan
    monitor_pvs:  Readbacks to plot / fit, can be single name or sequence
    n_avg:        Number of averages per setting
    cmd_pause:    After setting a command, wait this long before measuring
    pause:        Delay between measurements. 0 = Default will use camonitoring
    max_pause:    Max delay when relying on camonitoring
    poly_deg:     Order of polynomial to fit to data (None = no fit)
    plot:         True/False, whether to show plot
    stale:        True/False, whether to assume current value is stale after setting a command
    verbose:       True/False, prints per-setpoint progress.
    
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
        with plt.ioff():
            fig, axes = plt.subplots(1, len(names), sharex=True,
                                     squeeze=False,
                                     figsize=(5.0 * len(names), 5.0))
        for name, ax in zip(names, axes[0, :]):
            live_plots.append(LivePlot(ylabel=name, ax=ax))
        for ax in axes[0, :]:
            ax.set_xlabel(cmd_pv)
        fig.tight_layout()
        display_canvas(fig)
        # mouse events sent to a blocked kernel queue up and replay as
        # chaos after the scan — freeze the plot until we're done
        set_plot_interactive(fig, False)

    try:
        with restore_pvs(cmd_pv):
            for i, sp in enumerate(setpoints):
                if verbose:
                    print(f"[{i + 1}/{n_pts}] {cmd_pv} = {sp:g}")
                caput(cmd_pv, sp)
                time.sleep(cmd_pause)
                avg[i], std[i] = caget(names, n_avg=n_avg, pause=pause,
                                       max_pause=max_pause, stale=stale,
                                       return_std=True)
                for k, lp in enumerate(live_plots):
                    lp.update(setpoints[:i + 1], avg[:i + 1, k],
                              y_err=std[:i + 1, k])
    finally:
        if plot:
            set_plot_interactive(fig, True)

    fits = {}
    if poly_deg is not None:
        for k, name in enumerate(names):
            coeffs, errs, _ = polyfit_weights(setpoints, avg[:, k],
                                              std[:, k], poly_deg)
            fits[name] = (coeffs, errs)
            if live_plots:
                lp = live_plots[k]
                xs = np.linspace(setpoints.min(), setpoints.max(), 200)
                lp.update(xs, np.polynomial.polynomial.polyval(xs, coeffs),
                          label="fit", style="k-")
                cs = ", ".join(f"{c:.4g}" for c in coeffs)
                es = ", ".join(f"{e:.2g}" for e in errs)
                lp.ax.set_title(f"[{cs}] ± [{es}]", fontsize=10)
                lp.refresh()

    return dict(cmd_pv=cmd_pv, setpoints=setpoints, monitor_pvs=names,
                avg=avg, std=std, fits=fits, live_plots=live_plots)
