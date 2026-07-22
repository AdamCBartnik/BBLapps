r"""
get_todays_directory()

Returns the path to a day's data directory, following the same convention as
the MATLAB get_todays_directory() utility:

    Windows : \\samba\bbl_online\beamdata\YYYY\MM\YYYY-MM-DD\
    Linux   : /nfs/bbl/online/beamdata/YYYY/MM/YYYY-MM-DD/

A cron job creates these directories each day; this function only constructs
the path — it does not create anything.

Parameters
----------
day : optional
    Which day's directory to return:
      * None (default) -- today (offset by n_relative_day)
      * int            -- a relative-day offset (0 = today, -1 = yesterday,
                          ...); positive values raise (no directories exist
                          for the future)
      * str            -- an explicit date in almost any format, e.g.
                          '07-20-2026', '2026-07-20', '7/20/26',
                          'July 20 2026' (month-first for ambiguous dates,
                          matching MATLAB on a US locale; uses python-dateutil
                          when available, otherwise a set of common formats)
      * datetime.date / datetime.datetime -- used directly
n_relative_day : int, optional
    Legacy relative-day offset, used only when `day` is None.

Returns
-------
pathlib.Path
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_FUTURE_MSG = (
    "Life can only be understood backwards; but it must be lived forwards."
)

# Fallback date formats (month-first for ambiguous ones, matching MATLAB's
# datenum on a US locale), used only when python-dateutil isn't installed.
_FALLBACK_FORMATS = (
    "%m-%d-%Y", "%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d",
    "%m-%d-%y", "%m/%d/%y", "%Y%m%d",
    "%B %d %Y", "%b %d %Y", "%B %d, %Y", "%b %d, %Y",
    "%d %B %Y", "%d %b %Y",
)


def _parse_date_string(s: str) -> date:
    s = s.strip()
    try:
        from dateutil import parser as _dateutil_parser
    except ImportError:
        _dateutil_parser = None

    if _dateutil_parser is not None:
        try:
            return _dateutil_parser.parse(s).date()
        except (ValueError, OverflowError) as e:
            raise ValueError(f"could not parse date {s!r}") from e

    for fmt in _FALLBACK_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(
        f"could not parse date {s!r} (install python-dateutil for more "
        f"flexible parsing, or use one of: {', '.join(_FALLBACK_FORMATS)})"
    )


def _resolve_date(day, n_relative_day: int) -> date:
    if day is None:
        if n_relative_day > 0:
            raise ValueError(_FUTURE_MSG)
        return date.today() + timedelta(days=n_relative_day)
    # bool is a subclass of int -- reject it before the int branch
    if isinstance(day, bool):
        raise TypeError(f"day must be a date, string, or int, not {day!r}")
    if isinstance(day, int):
        if day > 0:
            raise ValueError(_FUTURE_MSG)
        return date.today() + timedelta(days=day)
    if isinstance(day, datetime):
        return day.date()
    if isinstance(day, date):
        return day
    if isinstance(day, str):
        return _parse_date_string(day)
    raise TypeError(
        f"day must be None, an int, a date/datetime, or a string, "
        f"not {type(day).__name__}"
    )


def get_todays_directory(day=None, n_relative_day: int = 0) -> Path:
    d = _resolve_date(day, n_relative_day)

    if sys.platform.startswith("win"):
        base = Path(r"\\samba\bbl_online\beamdata")
    else:
        base = Path("/nfs/bbl/online/beamdata")

    return base / d.strftime("%Y") / d.strftime("%m") / d.strftime("%Y-%m-%d")
