"""
Live-updating matplotlib plots for scan scripts in JupyterLab.

Use the ipympl backend (`%matplotlib widget` at the top of the notebook):
updates pushed from inside a blocking scan loop then appear immediately,
and zooming/panning with the figure toolbar is preserved across updates.

    lp = LivePlot(xlabel='corrector (A)', ylabel='centroid x (mm)')
    for i in range(n):
        ...measure point i...
        lp.update(x[:i+1], y[:i+1], y_err=err[:i+1])
    coeffs, coeff_errs = lp.fit(deg=1)
"""
import numpy as np
import matplotlib.pyplot as plt

from .fitting import polyfit_weights


def display_canvas(fig):
    """Display an ipympl figure widget immediately (works mid-cell).

    A figure created inside a running cell is normally only shown when
    the cell ends — useless for a scan that plots as it goes.  No-op
    outside Jupyter (plain scripts, Agg).
    """
    if not hasattr(fig.canvas, "_model_id"):
        return  # not a Jupyter widget canvas — auto-display handles it
    try:
        from IPython.display import display
        display(fig.canvas)
    except Exception:
        pass


class LivePlot:
    """A matplotlib (error bar) plot that updates in place during a scan.

    Pass the full data arrays to update() each time — it replaces what is
    drawn rather than appending.  Multiple traces on the same axes are
    supported via the label argument.
    """

    def __init__(self, xlabel="", ylabel="", title="", ax=None,
                 style="ro", capsize=3):
        if ax is None:
            # ioff: keep pyplot from ALSO auto-displaying the figure at the
            # end of the cell (we display the widget ourselves, right now)
            with plt.ioff():
                self.fig, self.ax = plt.subplots()
            display_canvas(self.fig)
        else:
            self.ax = ax
            self.fig = ax.figure
        if xlabel:
            self.ax.set_xlabel(xlabel)
        if ylabel:
            self.ax.set_ylabel(ylabel)
        if title:
            self.ax.set_title(title)
        self._style = style
        self._capsize = capsize
        self._traces = {}      # label -> dict(artist, x, y, y_err, style)
        self._fit_lines = {}   # label -> Line2D of the fit overlay
        self.fit_result = None  # (coeffs, coeff_errs) of the last fit()

    def update(self, x, y, y_err=None, label=None, style=None):
        """Replace the plotted data for this trace and redraw."""
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        if y_err is not None:
            y_err = np.asarray(y_err, dtype=float)

        old = self._traces.pop(label, None)
        if old is not None:
            old["artist"].remove()
        if style is None:
            style = old["style"] if old else self._style

        # ErrorbarContainer can't have its data replaced in place, so we
        # rebuild the artist each update (cheap at scan cadence)
        if y_err is None:
            artist, = self.ax.plot(x, y, style)
        else:
            artist = self.ax.errorbar(x, y, yerr=y_err, fmt=style,
                                      capsize=self._capsize)
        self._traces[label] = dict(artist=artist, x=x, y=y, y_err=y_err,
                                   style=style)
        self.refresh()

    def fit(self, deg=1, label=None):
        """Weighted polynomial fit of the current data, drawn as an overlay.

        Uses polyfit_weights (y_err from the last update() as absolute
        errors).  Title shows the fit as [c0, c1, ...] ± [e0, e1, ...],
        lowest power first (the numpy.polynomial convention: coeffs[0]
        constant, coeffs[1] slope, ...).  Returns (coeffs, coeff_errs) in
        that same order; also stored as self.fit_result.  May be called
        repeatedly — the overlay and title are replaced, not stacked.
        """
        tr = self._traces[label]
        coeffs, errs, _ = polyfit_weights(tr["x"], tr["y"], tr["y_err"], deg)

        xs = np.linspace(np.min(tr["x"]), np.max(tr["x"]), 200)
        ys = np.polynomial.polynomial.polyval(xs, coeffs)
        line = self._fit_lines.get(label)
        if line is None:
            line, = self.ax.plot(xs, ys, "k-")
            self._fit_lines[label] = line
        else:
            line.set_data(xs, ys)

        cs = ", ".join(f"{c:.4g}" for c in coeffs)
        es = ", ".join(f"{e:.2g}" for e in errs)
        self.ax.set_title(f"[{cs}] ± [{es}]", fontsize=10)

        self.fit_result = (coeffs, errs)
        self.refresh()
        return coeffs, errs

    def refresh(self):
        """Redraw now, even from inside a blocking loop."""
        # respect toolbar zoom/pan: those turn autoscale off
        if self.ax.get_autoscalex_on() or self.ax.get_autoscaley_on():
            self.ax.relim()
            self.ax.autoscale_view()
        # A synchronous draw(), NOT draw_idle(): under ipympl, draw_idle
        # needs a kernel<->browser round trip that can't complete while a
        # cell is blocked in a scan loop, so nothing would appear until
        # the scan finished. draw() renders in the kernel and pushes the
        # frame to the browser directly.
        self.fig.canvas.draw()
        try:
            self.fig.canvas.flush_events()
        except NotImplementedError:
            pass  # backends without an event loop (e.g. Agg)
