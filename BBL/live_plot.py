"""
Live-updating matplotlib plots for scan scripts in JupyterLab.

Use the ipympl backend (`%matplotlib widget` at the top of the notebook):
updates pushed from inside a blocking scan loop then appear immediately,
and zooming/panning with the figure toolbar is preserved across updates.

    lp = LivePlot(xlabel='corrector (A)', ylabel='centroid x (mm)')
    for i in range(n):
        ...measure point i...
        lp.update(x[:i+1], y[:i+1], y_err=err[:i+1])

Fits and other overlays are the caller's business: compute them
externally and pass them in as additional labeled traces, e.g.
lp.update(xs, ys, label='fit', style='k-').
"""
import numpy as np
import matplotlib.pyplot as plt


def set_plot_interactive(fig, enabled=True):
    """Enable/disable ALL mouse interaction with an ipympl figure.

    Interacting with a plot (zooming, clicking, even hovering) while a
    scan has the kernel blocked queues the mouse events browser-side
    traffic up in the kernel's message queue — they interfere with the
    live frame updates and then replay as chaos when the cell ends.
    Disabling puts pointer-events: none on the canvas widget, so the
    browser sends nothing at all during the scan.  No-op outside
    Jupyter (plain scripts, Agg).
    """
    canvas = fig.canvas
    if not hasattr(canvas, "add_class"):
        return  # not a Jupyter widget canvas
    if enabled:
        canvas.remove_class("bbl-noninteract")
    else:
        try:
            from IPython.display import HTML, display
            display(HTML(
                "<style>.bbl-noninteract { pointer-events: none; }</style>"))
        except Exception:
            pass
        canvas.add_class("bbl-noninteract")


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

    def set_interactive(self, enabled=True):
        """Freeze/unfreeze mouse interaction — see set_plot_interactive."""
        set_plot_interactive(self.fig, enabled)

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
