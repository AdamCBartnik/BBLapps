r"""
Example photocathode mask layouts, ported from the MATLAB model_qe_map.

These are ordinary data, not built into the model -- pass one to
bbl.cnf.model_qe_map, or write your own in the same form:

    {"period": (px, py) or None,
     "shapes": [
         {"type": "rect",    "center": (x, y), "size": (w, h), "angle": 0},
         {"type": "circle",  "center": (x, y), "diameter": d},
         {"type": "ellipse", "center": (x, y), "size": (a, b), "angle": 0},
         {"type": "polygon", "center": (x, y), "points": [(x, y), ...]},
     ]}

Circles take a DIAMETER, matching the MATLAB shape lists where the fourth
element was halved on use. Rect/ellipse "size" is full width and height.
Polygon points are relative to the shape's "center".

Lengths are whatever unit the mask was drawn in -- microns for these.
"""

# Dense test mask: a big square plus dots spanning 0.5 to 50 um.
DENSE = {
    "period": (200.0, 200.0),
    "shapes": [
        {"type": "rect",   "center": (0.0, 0.0),      "size": (100.0, 100.0)},
        {"type": "circle", "center": (0.0, 67.5),     "diameter": 1.0},
        {"type": "circle", "center": (0.0, -67.5),    "diameter": 1.0},
        {"type": "circle", "center": (100.0, -51.6667), "diameter": 0.5},
        {"type": "circle", "center": (100.0, 51.6667),  "diameter": 0.5},
        {"type": "circle", "center": (100.0, 0.0),    "diameter": 10.0},
        {"type": "circle", "center": (0.0, -100.0),   "diameter": 30.0},
        {"type": "circle", "center": (100.0, -100.0), "diameter": 50.0},
    ],
}

LESS_DENSE = {
    "period": (1000.0, 1000.0),
    "shapes": [
        {"type": "rect",   "center": (0.0, 0.0),    "size": (250.0, 400.0)},
        {"type": "circle", "center": (500.0, 500.0),  "diameter": 50.0},
        {"type": "circle", "center": (0.0, 500.0),    "diameter": 30.0},
        {"type": "circle", "center": (500.0, -200.0), "diameter": 0.5},
        {"type": "circle", "center": (500.0, 0.0),    "diameter": 1.0},
        {"type": "circle", "center": (500.0, 200.0),  "diameter": 2.0},
    ],
}

# A 3x3 grid of dots spanning 0.02 to 10 um -- the case that defeats
# rasterisation, since the smallest are ~100x below a typical pixel.
SID = {
    "period": (10000.0 / 3.0, 10000.0 / 3.0),
    "shapes": [
        {"type": "circle", "center": (-100.0, 100.0), "diameter": 0.1},
        {"type": "circle", "center": (0.0, 100.0),    "diameter": 0.05},
        {"type": "circle", "center": (100.0, 100.0),  "diameter": 0.02},
        {"type": "circle", "center": (-100.0, 0.0),   "diameter": 1.0},
        {"type": "circle", "center": (0.0, 0.0),      "diameter": 0.5},
        {"type": "circle", "center": (100.0, 0.0),    "diameter": 0.2},
        {"type": "circle", "center": (-100.0, -100.0), "diameter": 10.0},
        {"type": "circle", "center": (0.0, -100.0),   "diameter": 5.0},
        {"type": "circle", "center": (100.0, -100.0), "diameter": 2.0},
    ],
}


def _ppgun2():
    """3x3 dot grid inside a square frame of bars, with alignment ticks."""
    sizes = [3.0, 1.5, 4.0,
             0.75, 5.0, 1.0,
             2.0, 0.5, 2.5]
    step = 50.0
    shapes = []
    for ii in (-1, 0, 1):
        for jj in (-1, 0, 1):
            k = (-ii + 1) * 3 + (jj + 1)
            shapes.append({"type": "circle",
                           "center": (jj * step, ii * step),
                           "diameter": sizes[k]})
    rw, rh = 500.0, 100.0
    shapes += [
        {"type": "rect", "center": (rw * 0.5 + step * 2, 0.0), "size": (rw, rh)},
        {"type": "rect", "center": (-rw * 0.5 - step * 2, 0.0), "size": (rw, rh)},
        {"type": "rect", "center": (0.0, rw * 0.5 + step * 2), "size": (rh, rw)},
        {"type": "rect", "center": (0.0, -rw * 0.5 - step * 2), "size": (rh, rw)},
        {"type": "rect", "center": (-step * 2 + 5, 0.0), "size": (10.0, 5.0)},
        {"type": "rect", "center": (0.0, -step * 2 + 5), "size": (5.0, 10.0)},
        {"type": "rect", "center": (step * 2 - 5, 0.0), "size": (10.0, 5.0)},
        {"type": "rect", "center": (0.0, step * 2 - 5), "size": (5.0, 10.0)},
    ]
    return {"period": (2000.0, 2000.0), "shapes": shapes}


PPGUN2 = _ppgun2()


def _ppgun1():
    """Central dot, a ring of dots at 20 um radius, and four triangles.

    The triangles are why this one exists: MATLAB could not blur them at
    all ("Cannot blur triangles yet"), because a gaussian-convolved
    triangle has no elementary real-space form. Its Fourier transform is
    elementary, so here they cost the same as anything else.
    """
    import math
    shapes = [{"type": "circle", "center": (0.0, 0.0), "diameter": 5.0}]
    sizes = [3.0, 2.0, 1.5, 1.0, 0.75, 0.5]
    for i, d in enumerate(sizes):
        th = math.radians(i * 360.0 / len(sizes))
        shapes.append({"type": "circle",
                       "center": (20.0 * math.cos(th), 20.0 * math.sin(th)),
                       "diameter": d})
    tw, th_ = 300.0, 400.0
    tri = [(0.0, 0.0), (tw, th_ / 2), (tw, -th_ / 2)]
    for k in range(4):
        shapes.append({"type": "polygon", "center": (0.0, 0.0),
                       "points": tri, "angle": 45.0 + k * 90.0})
    return {"period": (2000.0, 2000.0), "shapes": shapes}


PPGUN1 = _ppgun1()

ALL = {"dense": DENSE, "less_dense": LESS_DENSE, "sid": SID,
       "ppgun1": PPGUN1, "ppgun2": PPGUN2}
