"""
BBL — shared Python utilities for Bright Beams Lab scripts and notebooks.

Typical notebook use:

    %matplotlib widget
    import BBL as bbl

    lp = bbl.LivePlot(xlabel='...', ylabel='...')
    data = bbl.measure_trend('SOME_cmd', setpoints, ['SOME_x_avg'])

Submodules are imported lazily on first attribute access, so e.g.
beamview can use BBL.today on a machine without matplotlib or pyepics.
"""
import importlib

_lazy = {
    "get_colormap": ".get_colormap",
    "get_todays_directory": ".today",
    "get_pv": ".pv_tools",
    "get_pv_avg": ".pv_tools",
    "restore_pvs": ".pv_tools",
    "LivePlot": ".live_plot",
    "polyfit_weights": ".fitting",
    "measure_trend": ".measure_trend",
}

__all__ = sorted(_lazy)


def __getattr__(name):
    if name in _lazy:
        module = importlib.import_module(_lazy[name], __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_lazy))
