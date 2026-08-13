r"""
Field maps for the beamline magnets.

    import BBL as bbl
    z, bz = bbl.fieldmaps.load_onaxis_field('solenoid_R128_sg.gdf')

z is in meters and bz in Tesla per amp of coil current, so a scan at
current I sees bz * I. Both are returned on the file's own z grid, which
must be uniform -- the transfer-matrix integration in bbl.solenoid steps
along it assuming constant dz.

Field maps live outside the repo: *.gdf is gitignored, since they are
large and per-magnet.
"""
import numpy as np


def load_onaxis_field(gdf_path):
    """Load an on-axis Bz(z) field map (T per A) from a .gdf file.

    Handles both a 1D map (blocks 'Z', 'Bz') and a 2D (r, z) map (blocks
    'R', 'Z', 'Bz', ...), extracting the on-axis (smallest |R|) slice in
    the latter case.  Returns (z, bz), sorted by z, on a uniform grid.
    """
    import easygdf

    d = easygdf.load(str(gdf_path))
    blocks = {b["name"].strip().lower(): np.asarray(b["value"], dtype=float)
              for b in d["blocks"]}
    if "z" not in blocks or "bz" not in blocks:
        raise ValueError(f"{gdf_path}: expected GDF blocks 'Z' and 'Bz'; "
                         f"found {list(blocks)}")
    z, bz = blocks["z"], blocks["bz"]

    if "r" in blocks:
        r = blocks["r"]
        r0 = r[np.argmin(np.abs(r))]
        if not np.isclose(r0, 0.0, atol=1e-6):
            print(f"[solenoid] WARNING: field map's smallest |R| is "
                  f"{r0:g} m, not exactly 0 — using it as the on-axis "
                  "approximation")
        mask = np.isclose(r, r0, atol=1e-9)
        z, bz = z[mask], bz[mask]

    order = np.argsort(z)
    z, bz = z[order], bz[order]
    dz = np.diff(z)
    if not np.allclose(dz, dz[0], rtol=1e-3):
        raise ValueError(f"{gdf_path}: on-axis Z grid is not uniform "
                         "(required for the transfer-matrix integration)")
    return z, bz
