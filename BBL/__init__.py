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
import os

# Must happen before pyepics/epics ever connects a channel in this process
# (libca reads it at init) -- default is far too small for a multi-megapixel
# image waveform (e.g. image1:ArrayData). Since BBL's submodules are all
# lazily imported, THIS is the one place guaranteed to run before any of
# them touch epics, regardless of which BBL name gets accessed first.
os.environ.setdefault("EPICS_CA_MAX_ARRAY_BYTES", "40000000")

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
    "plot_frame": ".get_frame",
    "ssss": ".ssss",
    "next_ssss_stem": ".ssss",
    # module name deliberately differs from the function name, which side-
    # steps the submodule-shadowing trap described in __getattr__ below
    "screen_sensitivity_correction": ".flatfield",
}

__all__ = sorted(_lazy)


def __getattr__(name):
    if name in _lazy:
        modname = _lazy[name]
        module = importlib.import_module(modname, __name__)
        # Cache the resolved objects in the package namespace.  Not just an
        # optimization: importing a submodule binds it as a package attribute,
        # so where a function shares its module's name (measure_trend,
        # get_colormap, solenoid_scan, center_laser_in_gun, get_frame, ssss)
        # the module would shadow the function on every access after the first
        # ('module' object is not callable).  This overwrite puts the function
        # back on top -- as long as this __getattr__ is what triggers the
        # import.
        #
        # Cache EVERY lazy name belonging to this module, not just the one
        # asked for.  Resolving only the requested name leaves a SIBLING
        # shadowed: bbl.plot_frame() imports BBL.get_frame (binding the
        # module over the function of the same name) and, if only
        # 'plot_frame' were cached, a later bbl.get_frame() would find the
        # module and raise.  Order-dependent, so it hid for a long time.
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
        for lazy_name, lazy_mod in _lazy.items():
            if lazy_mod == modname and hasattr(module, lazy_name):
                globals()[lazy_name] = getattr(module, lazy_name)
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_lazy))
