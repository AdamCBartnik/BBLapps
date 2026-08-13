"""
get_colormap(name, m, p)  —  Python port of the MATLAB get_colormap utility.

    get_colormap()            → sorted list of available colormap names
    get_colormap(name)        → a matplotlib Colormap, ready for cmap=
    get_colormap(name, m)     → resampled to m entries
    get_colormap(name, m, p)  → resampled with power-law exponent p

    get_colormap(name, return_list=True)  → the raw (m, 3) float array

Almost every caller is handing the result straight to matplotlib, so that is
what comes back by default. `return_list=True` gives the underlying RGB array
instead, for consumers that build their own lookup table -- beamview feeds it
to pyqtgraph's setLookupTable as (256, 4) uint8.

Append '_r' to any name to reverse the colormap.

Custom colormaps are loaded from the colormaps/ subdirectory (text files with
256 rows of R G B values in [0, 1]).  Built-in matplotlib colormaps (bone,
gray, hot, jet) are also available.
"""

from pathlib import Path
import numpy as np

_DIR = Path(__file__).parent / "colormaps"
_BUILTINS = ["bone", "gray", "hot", "jet"]


def get_colormap(name=None, m=256, p=1.0, return_list=False):
    if name is None:                    # the name list, whatever return_list says
        txt_names = [f.stem for f in sorted(_DIR.glob("*.txt"))]
        all_names = txt_names + _BUILTINS
        capitalized = [n[0].upper() + n[1:] for n in all_names]
        return sorted(capitalized)

    reverse = False
    key = name.lower()
    if key.endswith("_r"):
        key = key[:-2]
        reverse = True

    txt_path = _DIR / f"{key}.txt"
    if txt_path.exists():
        cm = np.loadtxt(txt_path)          # (N, 3), float 0-1
    else:
        import matplotlib.pyplot as plt
        cmap = plt.get_cmap(key)
        n = cmap.N if cmap.N < 65536 else 256
        cm = cmap(np.linspace(0, 1, n))[:, :3]

    if m != len(cm) or p != 1.0:
        cm = _resample(cm, m, p)

    if reverse:
        cm = cm[::-1]

    if return_list:
        return cm
    from matplotlib.colors import ListedColormap
    # keep the name the caller asked for, '_r' and all, so the repr is useful
    return ListedColormap(cm, name=name)


def _resample(cm, m, p=1.0):
    """Resample via HSV interpolation with optional power-law (matches MATLAB)."""
    import matplotlib.colors as mc
    n = len(cm)
    hsv = mc.rgb_to_hsv(cm)
    hsv[:, 0] = np.unwrap(2 * np.pi * hsv[:, 0]) / (2 * np.pi)
    x_old = np.linspace(0, 1, n) ** p
    x_new = np.linspace(0, 1, m)
    hsv_new = np.column_stack([
        np.interp(x_new, x_old, hsv[:, i]) for i in range(3)
    ])
    hsv_new[:, 0] %= 1.0
    return np.clip(mc.hsv_to_rgb(hsv_new), 0, 1)
