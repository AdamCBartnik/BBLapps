"""
pyepics helpers shared by scan scripts.

- get_pv(name)        cached, auto-monitored epics.PV
- get_pv_avg(...)     averaged read that waits for fresh camonitor values
- restore_pvs(...)    context manager: put values back on exit / Ctrl-C
"""
import time
from contextlib import contextmanager

import numpy as np
import epics

_pv_cache = {}
_update_counts = {}   # pvname -> number of monitor updates seen


def _count_update(pvname=None, **kws):
    # runs in the CA monitor thread; dict ops are GIL-atomic
    _update_counts[pvname] = _update_counts.get(pvname, 0) + 1


def get_pv(name):
    """Return a cached, auto-monitored epics.PV (created on first use)."""
    pv = _pv_cache.get(name)
    if pv is None:
        pv = epics.PV(name, auto_monitor=True, callback=_count_update)
        _pv_cache[name] = pv
    return pv


def _connect(pvs, timeout=5.0):
    for pv in pvs:
        if not pv.wait_for_connection(timeout=timeout):
            raise TimeoutError(f"PV not connected: {pv.pvname}")


def get_pv_avg(pv_names, n_avg=1, pause=0.0, max_pause=5.0):
    """Average n_avg reads of one or more PVs; returns (avg, std).

    Each sample waits at least `pause` seconds AND for a fresh monitor
    update on every PV arriving after the sample started — the cached
    value is vetoed (labca veto_current_data / wait_until_new_data
    pattern), so consecutive samples are new measurements, not re-reads
    of a stale value.  If no update arrives within `max_pause` seconds
    the sample proceeds anyway with the latest known value.

    pv_names may be a single name (returns scalar avg/std) or a sequence
    (returns arrays in the same order).  std is the sample standard
    deviation (ddof=1); zero when n_avg == 1.
    """
    single = isinstance(pv_names, str)
    names = [pv_names] if single else list(pv_names)
    pvs = [get_pv(n) for n in names]
    _connect(pvs)

    samples = np.full((n_avg, len(names)), np.nan)
    for i in range(n_avg):
        marks = {n: _update_counts.get(n, 0) for n in names}
        t0 = time.monotonic()
        while True:
            elapsed = time.monotonic() - t0
            if elapsed >= max_pause:
                break
            if (elapsed >= pause
                    and all(_update_counts.get(n, 0) > marks[n]
                            for n in names)):
                break
            time.sleep(0.01)
        for k, pv in enumerate(pvs):
            v = pv.value
            samples[i, k] = np.nan if v is None else float(v)

    avg = np.nanmean(samples, axis=0)
    if n_avg > 1:
        std = np.nanstd(samples, axis=0, ddof=1)
    else:
        std = np.zeros(len(names))
    if single:
        return float(avg[0]), float(std[0])
    return avg, std


@contextmanager
def restore_pvs(*pv_names):
    """Record PV values on entry, write them back on exit.

    The restore runs on any exit — normal completion, exception, or
    Ctrl-C / notebook kernel interrupt (the MATLAB onCleanup pattern):

        with restore_pvs('MA1CHA01_cmd', 'MA1CVA01_cmd'):
            ... scan ...

    Yields a dict {pv_name: initial_value}.
    """
    pvs = [get_pv(n) for n in pv_names]
    _connect(pvs)
    initial = [pv.get() for pv in pvs]
    try:
        yield dict(zip(pv_names, initial))
    finally:
        print("Restoring initial PV values...")
        for pv, val in zip(pvs, initial):
            if val is not None:
                pv.put(val)
