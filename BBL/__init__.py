"""
BBL — shared Python utilities for Bright Beams Lab scripts and notebooks.

Organised into subpackages, the way scipy is:

    import BBL as bbl

    bbl.epics.caget('SOME:PV')
    lp = bbl.plot.LivePlot(xlabel='...', ylabel='...')
    frame = bbl.image.get_frame('B24Screen1')
    data = bbl.utilities.measure_trend('SOME_cmd', setpoints, ['SOME_x'])
    bbl.solenoid.solenoid_scan(pvs, currents, fieldmap, drift_length)
    bbl.gun.center_laser_in_gun(pvs)

or pull the names you use often into the notebook's own namespace:

    from BBL.epics import caget, caput

    epics       caget, caput, restore_pvs
    plot        LivePlot, warmup, get_colormap
    image       get_frame, plot_frame, screen_sensitivity_correction
    utilities   polyfit_weights, get_todays_directory, ssss,
                next_ssss_stem, measure_trend
    gun         center_laser_in_gun, fit_gun_aberration
    solenoid    solenoid_scan, fit_solenoid_scan
    fieldmaps   load_onaxis_field

Everything else is private and lives with whatever uses it (_physics.py,
the pv_tools sampling internals, live_plot's display helpers).

Nothing is imported until it is touched. That is not just for startup
time: beamview imports BBL.utilities for its snapshot numbering, and lab
machines run beamview WITHOUT matplotlib installed -- so importing BBL
must not drag in bbl.plot.

One naming rule keeps this simple, and it is worth stating because
breaking it caused two real bugs before the package was reorganised: NO
MODULE MAY SHARE A NAME WITH A FUNCTION IT EXPORTS. Importing a submodule
binds it as an attribute of its package, so a module named after its own
function shadows that function -- which is why get_frame.py is now
frames.py, ssss.py is saving.py, and so on. Subpackage names (epics,
plot, image, ...) are domains and can never collide with function names,
so nothing here has to defend against it.
"""
import importlib
import os

# Must happen before pyepics/epics ever connects a channel in this process
# (libca reads it at init) -- the default is far too small for a
# multi-megapixel image waveform (e.g. image1:ArrayData). Since everything
# below is lazily imported, THIS is the one place guaranteed to run before
# any of it touches epics, whichever name is accessed first.
os.environ.setdefault("EPICS_CA_MAX_ARRAY_BYTES", "40000000")

_subpackages = (
    "epics",
    "fieldmaps",
    "gun",
    "image",
    "plot",
    "solenoid",
    "utilities",
)

__all__ = list(_subpackages)


def __getattr__(name):
    if name in _subpackages:
        return importlib.import_module(f".{name}", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}. "
                         f"BBL is organised into subpackages: "
                         f"{', '.join(_subpackages)}")


def __dir__():
    return sorted(__all__)
