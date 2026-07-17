"""
pyepics helpers shared by scan scripts.

- get_pv(name)        cached, auto-monitored epics.PV
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


def _sample(names, n_avg, pause, max_pause, fresh):
    """Sample the named PVs n_avg times; always returns (avg, std) arrays.

    With fresh=True each sample waits at least `pause` seconds AND for a
    camonitor update on every PV arriving after the sample started — the
    cached value is vetoed (labca veto_current_data / wait_until_new_data
    pattern), so consecutive samples are new measurements, not re-reads
    of a stale value.  If any connected PV delivers no update within
    `max_pause` seconds, the whole read BAILS OUT and returns all-NaN
    (with the stale PVs printed): no stale data, and no repeating the
    wait for every remaining sample.

    Unreachable PVs yield NaN.  std is the sample standard deviation
    (ddof=1); zeros when n_avg == 1.
    """
    pvs = [get_pv(n) for n in names]
    connected = _connect(pvs, required=False)

    samples = np.full((n_avg, len(names)), np.nan)
    for i in range(n_avg):
        if fresh:
            marks = {n: _update_counts.get(n, 0) for n in names}
            t0 = time.monotonic()
            while True:
                elapsed = time.monotonic() - t0
                if (elapsed >= pause
                        and all(not c or _update_counts.get(n, 0) > marks[n]
                                for n, c in zip(names, connected))):
                    break
                if elapsed >= max_pause:
                    stale = [n for n, c in zip(names, connected)
                             if c and _update_counts.get(n, 0) <= marks[n]]
                    print(f"[caget] no fresh update within {max_pause:g} s "
                          f"({', '.join(stale)}) — returning nan")
                    nan = np.full(len(names), np.nan)
                    return nan, nan.copy()
                time.sleep(0.01)
        elif pause > 0:
            time.sleep(pause)
        for k, (pv, c) in enumerate(zip(pvs, connected)):
            v = pv.value if c else None
            samples[i, k] = np.nan if v is None else float(v)

    # an unreachable PV leaves an all-NaN column; nanmean/nanstd warn on
    # those but correctly return NaN, which is exactly what we want
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        avg = np.nanmean(samples, axis=0)
        if n_avg > 1:
            std = np.nanstd(samples, axis=0, ddof=1)
        else:
            std = np.zeros(len(names))
    return avg, std


def caget(pv_names, n_avg=1, pause=0.0, max_pause=5.0, fresh=None):
    """Read one or more PVs via the monitor cache (never raises).

    caget('PV') returns the current value immediately, like lcaGet;
    an unreachable PV gives NaN (after a connection wait, first time).
    A sequence of names returns an array of values.

    n_avg > 1 averages repeated samples and returns (avg, std) instead.
    How sampling is paced depends on `pause`:

      pause == 0 (default): camonitor-paced.  Each sample waits for a
        new update arriving after the sample started (the fresh-update
        veto), so every sample is a genuinely new measurement.  If any
        PV goes `max_pause` seconds without an update the whole read
        bails out and returns NaN — see _sample.
      pause > 0: time-paced.  Samples are taken every `pause` seconds
        from the monitor cache, fresh or not — for records that only
        update when commanded.

    fresh overrides that default (None = veto only when n_avg > 1 and
    pause == 0): fresh=True demands new updates even with a pause (and
    for single reads), fresh=False free-runs on the cache.
    """
    single = isinstance(pv_names, str)
    names = [pv_names] if single else list(pv_names)
    if fresh is None:
        fresh = n_avg > 1 and pause == 0

    avg, std = _sample(names, n_avg, pause, max_pause, fresh)

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
    immediately, like lcaPutNoWait.  `timeout` bounds the wait.

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

    pvs = [get_pv(n) for n in names]
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
    pvs = [get_pv(n) for n in pv_names]
    _connect(pvs, required=True)
    initial = [pv.get() for pv in pvs]
    try:
        yield dict(zip(pv_names, initial))
    finally:
        print("Restoring initial PV values...")
        for pv, val in zip(pvs, initial):
            if val is not None:
                pv.put(val)
