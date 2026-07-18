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
    "caget": ".pv_tools",
    "caput": ".pv_tools",
    "restore_pvs": ".pv_tools",
    "LivePlot": ".live_plot",
    "set_plot_interactive": ".live_plot",
    "polyfit_weights": ".fitting",
    "measure_trend": ".measure_trend",
}

__all__ = sorted(_lazy)


def __getattr__(name):
    if name in _lazy:
        module = importlib.import_module(_lazy[name], __name__)
        attr = getattr(module, name)
        # Cache the resolved object in the package namespace.  Not just an
        # optimization: importing a submodule binds it as a package attribute,
        # so where a function shares its module's name (measure_trend,
        # get_colormap) the module would shadow the function on every access
        # after the first ('module' object is not callable).  This overwrite
        # puts the function back on top.
        globals()[name] = attr
        return attr
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_lazy))
