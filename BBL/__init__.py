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
    "center_laser_in_gun": ".center_laser_in_gun",
    "fit_gun_aberration": ".center_laser_in_gun",
    "caput": ".pv_tools",
    "restore_pvs": ".pv_tools",
    "LivePlot": ".live_plot",
    "set_plot_interactive": ".live_plot",
    "warmup": ".live_plot",
    "polyfit_weights": ".fitting",
    "measure_trend": ".measure_trend",
    "solenoid_scan": ".solenoid_scan",
    "fit_solenoid_scan": ".solenoid_scan",
    "load_onaxis_field": ".solenoid_scan",
    "get_frame": ".get_frame",
    "load_h5_frame": ".get_frame",
    "plot_frame": ".get_frame",
}

__all__ = sorted(_lazy)


def __getattr__(name):
    if name in _lazy:
        module = importlib.import_module(_lazy[name], __name__)
        attr = getattr(module, name)
        # Cache the resolved object in the package namespace.  Not just an
        # optimization: importing a submodule binds it as a package attribute,
        # so where a function shares its module's name (measure_trend,
        # get_colormap, solenoid_scan, center_laser_in_gun) the module would
        # shadow the function on every access after the first ('module'
        # object is not callable).  This overwrite puts the function back on
        # top -- as long as this __getattr__ is what triggers the import.
        #
        # KNOWN LIMITATION: this only works if THIS __getattr__ is what
        # first imports the submodule.  If a colliding submodule is instead
        # imported directly as the FIRST touch of it in the process, e.g.
        #   from BBL.solenoid_scan import load_onaxis_field   # first thing
        # Python's import system binds BBL.solenoid_scan = <the submodule>
        # as an unconditional final step of that statement -- nothing
        # running inside the submodule or in this __getattr__ can run
        # "after" that step to undo it, so bbl.solenoid_scan then stays a
        # module, not the function, for the rest of the process.  (Once the
        # submodule IS already imported -- e.g. after a prior bbl.X access
        # -- a later `from BBL.X import ...` is safe: Python's fast path
        # for an already-loaded module skips the re-bind.)  Simplest rule:
        # `import BBL as bbl; bbl.solenoid_scan(...)` always works.
        globals()[name] = attr
        return attr
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_lazy))
