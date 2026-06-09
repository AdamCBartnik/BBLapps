r"""
get_todays_directory()

Returns the path to today's data directory, following the same convention as
the MATLAB get_todays_directory() utility:

    Windows : \\samba\bbl_online\beamdata\YYYY\MM\YYYY-MM-DD\
    Linux   : /nfs/bbl/online/beamdata/YYYY/MM/YYYY-MM-DD/

A cron job creates these directories each day; this function only constructs
the path — it does not create anything.

Parameters
----------
n_relative_day : int, optional
    0 (default) = today, -1 = yesterday, etc.  Positive values raise an error
    (matching the spirit of the MATLAB version).

Returns
-------
pathlib.Path
"""

import sys
from datetime import date, timedelta
from pathlib import Path


def get_todays_directory(n_relative_day: int = 0) -> Path:
    if n_relative_day > 0:
        raise ValueError(
            "Life can only be understood backwards; but it must be lived forwards."
        )

    d = date.today() + timedelta(days=n_relative_day)
    yyyy = d.strftime("%Y")
    mm   = d.strftime("%m")
    dd   = d.strftime("%Y-%m-%d")

    if sys.platform.startswith("win"):
        base = Path(r"\\samba\bbl_online\beamdata")
    else:
        base = Path("/nfs/bbl/online/beamdata")

    return base / yyyy / mm / dd
