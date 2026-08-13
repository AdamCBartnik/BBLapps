"""CNF photocathode work: modelling a lithographic mask as a laser sees it."""
import importlib

_lazy = {
    "model_qe_map": ".qe_map",
    "patterns": ".patterns",
}

__all__ = sorted(_lazy)


def __getattr__(name):
    """Import the module holding the requested name, on first access.

    Same shape as scipy's own __init__: nothing is imported until it is
    touched, so e.g. beamview can use bbl.utilities.get_todays_directory on
    a machine with no matplotlib installed.

    No module in this package shares a name with a function it exports,
    which is what keeps this simple -- see BBL/__init__.py.
    """
    mod = _lazy.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if name == "patterns":          # a module, not a name inside one
        return importlib.import_module(mod, __name__)
    return getattr(importlib.import_module(mod, __name__), name)


def __dir__():
    return __all__
