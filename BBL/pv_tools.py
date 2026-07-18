"""
pyepics helpers shared by scan scripts.

- caget(...)          read via the monitor cache; averaging + fresh-update veto
- caput(...)          write; wait=True (lcaPut) or wait=False (lcaPutNoWait)
- restore_pvs(...)    context manager: put values back on exit / Ctrl-C
"""
import time
import warnings
from contextlib import contextmanager

import numpy as np
import epics

_CONNECT_TIMEOUT = 5.0

_pv_cache = {}
_update_counts = {}   # pvname -> number of monitor updates seen


def caget(pv_names, n_avg=1, pause=0.0, max_pause=5.0, stale=False):
    """Read one or more PVs via the monitor cache (never raises).

    caget('PV') returns the current value immediately
    an unreachable PV gives NaN (after a connection wait, first time).
    A sequence of names returns an array of values.

    n_avg > 1 averages repeated samples and returns (avg, std) instead.
    How sampling is paced depends on `pause`:

      pause == 0 (default): camonitor-paced.  Each sample waits for a
        new update arriving after the sample started.  If any
        PV goes `max_pause` seconds without an update the whole read
        bails out and returns NaN
      pause > 0: time-paced.  Samples are taken every `pause` seconds

    stale is used when the current value is assumed to be stale
    and either a new monitored value is needed or a single pause
    """
    single = isinstance(pv_names, str)
    names = [pv_names] if single else list(pv_names)

    if (stale):
        samples = _sample(names, n_avg+1, pause, max_pause)
        samples = samples[1:,:]
    else:
        samples = _sample(names, n_avg, pause, max_pause)

    # an unreachable PV leaves an all-NaN column; nanmean/nanstd warn on
    # those but correctly return NaN, which is exactly what we want
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        avg = np.nanmean(samples, axis=0)
        if n_avg > 1:
            std = np.nanstd(samples, axis=0)
        else:
            std = np.zeros(len(names))


    if n_avg > 1:
        if single:
            return float(avg[0]), float(std[0])
        return avg, std
    if single:
        return float(avg[0])
    return avg


def caput(pv_names, values, wait=True, timeout=5.0):
    """Write one or more PVs.

    wait=True (default) blocks until the IOC confirms the record has
    processed, like lcaPut; wait=False fires the write and returns
    immediately.  `timeout` bounds the wait.

    pv_names may be a single name or a sequence; a scalar value is
    broadcast across a sequence of names.  Returns True if every put was
    delivered (and, with wait=True, confirmed) — otherwise False, with
    the failure printed rather than raised.
    """
    single = isinstance(pv_names, str)
    names = [pv_names] if single else list(pv_names)
    if single:
        vals = [values]
    elif np.isscalar(values):
        vals = [values] * len(names)
    else:
        vals = list(values)
        if len(vals) != len(names):
            raise ValueError(f"{len(names)} PVs but {len(vals)} values")

    pvs = [_get_pv(n) for n in names]
    connected = _connect(pvs, required=False)

    ok = True
    for pv, c, v in zip(pvs, connected, vals):
        if not c:
            ok = False
            continue
        try:
            ret = pv.put(v, wait=wait, timeout=timeout)
            if wait and ret != 1:
                print(f"[caput] {pv.pvname} = {v}: not confirmed "
                      f"within {timeout} s")
                ok = False
        except Exception as e:
            print(f"[caput] {pv.pvname} = {v}: {e}")
            ok = False
    return ok


@contextmanager
def restore_pvs(*pv_names):
    """Record PV values on entry, write them back on exit.

    The restore runs on any exit — normal completion, exception, or
    Ctrl-C / notebook kernel interrupt (the MATLAB onCleanup pattern):

        with restore_pvs('MA1CHA01_cmd', 'MA1CVA01_cmd'):
            ... scan ...

    Yields a dict {pv_name: initial_value}.  Unlike caget/caput this
    RAISES if a PV doesn't connect: silently scanning something that
    can't be restored would be worse than stopping.
    """
    pvs = [_get_pv(n) for n in pv_names]
    _connect(pvs, required=True)
    initial = [pv.get() for pv in pvs]
    try:
        yield dict(zip(pv_names, initial))
    finally:
        print("Restoring initial PV values...")
        for pv, val in zip(pvs, initial):
            if val is not None:
                pv.put(val)



def _count_update(pvname=None, **kws):
    # runs in the CA monitor thread; dict ops are GIL-atomic
    _update_counts[pvname] = _update_counts.get(pvname, 0) + 1


def _get_pv(name):
    """Return a cached, auto-monitored epics.PV (created on first use)."""
    pv = _pv_cache.get(name)
    if pv is None:
        pv = epics.PV(name, auto_monitor=True, callback=_count_update)
        _pv_cache[name] = pv
    return pv


def _connect(pvs, required=True):
    """Wait for connections. required=True raises on failure; otherwise
    prints a warning and returns a list of connected flags."""
    ok = []
    for pv in pvs:
        connected = pv.wait_for_connection(timeout=_CONNECT_TIMEOUT)
        if not connected:
            if required:
                raise TimeoutError(f"PV not connected: {pv.pvname}")
            print(f"[caget/caput] PV not connected: {pv.pvname}")
        ok.append(connected)
    return ok


def _sample(names, n_avg, pause, max_pause):
    """Sample the named PVs n_avg times; always returns (avg, std) arrays.
    If pause = 0, relies on camonitor for timing, up to a max of max_pause
    """
    pvs = [_get_pv(n) for n in names]
    connected = _connect(pvs, required=False)
    
    samples = np.full((n_avg, len(names)), np.nan)
    for i in range(n_avg):
        for k, (pv, c) in enumerate(zip(pvs, connected)):
            v = pv.value if c else None
            samples[i, k] = np.nan if v is None else float(v)

        if (i < n_avg-1):
            if (pause > 0):
                time.sleep(pause)
            else:
                marks = {n: _update_counts.get(n, 0) for n in names}
                t0 = time.monotonic()
                while True:
                    if all(not c or _update_counts.get(n, 0) > marks[n]
                           for n, c in zip(names, connected)):
                        break
                    if time.monotonic() - t0 >= max_pause:
                        stale_pvs = [n for n, c in zip(names, connected)
                                     if c and _update_counts.get(n, 0) <= marks[n]]
                        print(f"[caget] no fresh update within {max_pause:g} s "
                              f"({', '.join(stale_pvs)}) — returning nan")
                        # same shape as the normal return, all NaN
                        return np.full((n_avg, len(names)), np.nan)
                    time.sleep(0.01)

    return samples
