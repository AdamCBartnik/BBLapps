"""Shared data-coordinate helpers for beamview's image views.

main_window and snapshot_window draw the same kind of image with the same
xx/yy conventions but share no base class (and snapshot_window can't
import main_window -- main_window creates it), so the subtle parts of
mapping data coordinates to pixel indices live here instead of being
duplicated.  They have drifted apart before.
"""
import numpy as np


def nearest_index(arr, v, n):
    """Index of the element of `arr` (pixel CENTRES) nearest to value `v`.

    `arr` is a uniformly spaced ramp, ascending or descending -- both of
    beamview's xx and yy are descending (see main_window._get_display_xy).
    `n` bounds the result, so a coordinate array longer than the image it
    describes can't index out of range.

    Do NOT use np.searchsorted here.  searchsorted finds the insertion
    BOUNDARY, not the nearest element: a point anywhere strictly between
    two centres snaps to the upper one, so every lookup was biased by half
    a pixel and the hover tooltip reported the neighbouring pixel's value
    over half of each pixel's area.  Rounding the offset in pixel units
    picks the genuinely nearest centre.  (Ties -- the cursor exactly on a
    pixel boundary -- go either way, which is fine: both neighbours are
    equidistant.)
    """
    if len(arr) < 2:
        return 0
    step = arr[1] - arr[0]
    return int(np.clip(int(np.round((v - arr[0]) / step)), 0, n - 1))
