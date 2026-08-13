"""model_qe_map checks, against closed forms rather than against itself.

A rectangle blurred by a gaussian has an exact elementary answer, and so
does its average over a pixel -- that is the reference here, and it tests
the whole chain at once: the analytic transforms, the FFT normalisation,
the pixel box filter and the grid alignment. Everything else is checked
against conservation of area, which holds for any blur.

Run: python BBL/cnf/test_qe_map.py
"""
import os
import sys
import time
from math import erf, exp, pi, sqrt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import numpy as np

import BBL as bbl
from BBL.cnf import patterns

mqm = bbl.cnf.model_qe_map
failures = []


def check(name, ok, detail=""):
    print(f"  {'ok ' if ok else 'BAD'}  {name}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(f"{name}  {detail}")


def _erf_edge_integral(u, c):
    """Antiderivative of erf(u/c), for averaging the exact answer."""
    return u * erf(u / c) + (c / sqrt(pi)) * exp(-(u / c) ** 2)


def exact_blurred_rect_pixel_mean(xc, w, sig, d):
    """Mean of the exact gaussian-blurred edge profile over one pixel.

    The blurred rect is P(x)Q(y) with
        P(x) = 1/2 [erf((x+w/2)/(sqrt(2)sig)) - erf((x-w/2)/(sqrt(2)sig))],
    and erf integrates in closed form, so the pixel average is exact too.
    """
    c = sqrt(2.0) * sig
    a, b = xc - d / 2.0, xc + d / 2.0
    hi = _erf_edge_integral(b + w / 2, c) - _erf_edge_integral(a + w / 2, c)
    lo = _erf_edge_integral(b - w / 2, c) - _erf_edge_integral(a - w / 2, c)
    return 0.5 * (hi - lo) / d


print("blurred rectangle vs the exact pixel-averaged erf product")
w, h = 40.0, 25.0
rect = {"period": None,
        "shapes": [{"type": "rect", "center": (0, 0), "size": (w, h)}]}
for sig, npix in ((6.0, 160), (2.0, 160), (15.0, 96)):
    M, x, y = mqm(rect, sigma=sig, extent=(160, 160), shape=(npix, npix),
                  verbose=False)
    d = x[1] - x[0]
    px = np.array([exact_blurred_rect_pixel_mean(xi, w, sig, d) for xi in x])
    py = np.array([exact_blurred_rect_pixel_mean(yi, h, sig, d) for yi in y])
    err = np.abs(M - np.outer(py, px)).max()
    check(f"sigma={sig:g} on {npix}x{npix}", err < 1e-9,
          f"max |diff| = {err:.2e}")

print()
print("elliptical and rotated laser")
su, sv = 9.0, 3.0
M, x, y = mqm(rect, sigma=(su, sv), extent=(160, 160), shape=(160, 160),
              verbose=False)
d = x[1] - x[0]
px = np.array([exact_blurred_rect_pixel_mean(xi, w, su, d) for xi in x])
py = np.array([exact_blurred_rect_pixel_mean(yi, h, sv, d) for yi in y])
err = np.abs(M - np.outer(py, px)).max()
check("separable elliptical blur is exact", err < 1e-9,
      f"max |diff| = {err:.2e}")

M90, _, _ = mqm(rect, sigma=(su, sv), laser_angle=90.0, extent=(160, 160),
                shape=(160, 160), verbose=False)
Msw, _, _ = mqm(rect, sigma=(sv, su), extent=(160, 160), shape=(160, 160),
                verbose=False)
check("90 deg rotation swaps sigma_u and sigma_v",
      np.abs(M90 - Msw).max() < 1e-12,
      f"max |diff| = {np.abs(M90 - Msw).max():.2e}")

square45 = {"period": None,
            "shapes": [{"type": "rect", "center": (0, 0), "size": (30.0, 30.0),
                        "angle": 45.0}]}
M, x, y = mqm(square45, sigma=5.0, extent=(160, 160), shape=(160, 160),
              verbose=False)
check("a 45 deg square is symmetric under transpose",
      np.abs(M - M.T).max() < 1e-12, f"max |diff| = {np.abs(M - M.T).max():.2e}")

print()
print("blur conserves total signal (= shape area), any sigma")
cases = [("disc d=20", {"type": "circle", "center": (0, 0), "diameter": 20.0},
          pi * 100),
         ("triangle", {"type": "polygon", "center": (0, 0),
                       "points": [(-20, -12), (20, -12), (0, 18)]}, 600.0),
         ("ellipse 30x12", {"type": "ellipse", "center": (0, 0),
                            "size": (30.0, 12.0), "angle": 25.0}, pi * 15 * 6),
         ("rect 40x25 at 30deg", {"type": "rect", "center": (0, 0),
                                  "size": (40.0, 25.0), "angle": 30.0}, 1000.0)]
for name, sh, area in cases:
    p = {"period": None, "shapes": [sh]}
    worst = 0.0
    for s in (2.0, 5.0, 12.0):
        M, x, y = mqm(p, sigma=s, extent=(240, 240), shape=(256, 256),
                      verbose=False)
        got = M.sum() * (x[1] - x[0]) * (y[1] - y[0])
        worst = max(worst, abs(got - area) / area)
    check(name, worst < 1e-9, f"worst relative error {worst:.1e}")

print()
print("sigma = 0: the layout itself, as fractional pixel coverage")
# With no gaussian the only band limit is the pixel's own box, which rolls
# off as 1/k, so the truncated series rings at the percent level. That is
# documented behaviour, and oversample is the lever that removes it.
disc = {"period": None,
        "shapes": [{"type": "circle", "center": (0, 0), "diameter": 60.0}]}
errs = {}
for over in (1, 4):
    M, x, y = mqm(disc, sigma=0.0, extent=(200, 200), shape=(64, 64),
                  oversample=over, verbose=False)
    got = M.sum() * (x[1] - x[0]) * (y[1] - y[0])
    errs[over] = abs(got - pi * 900) / (pi * 900)
    check(f"output is a valid coverage fraction at oversample={over}",
          M.min() >= 0.0 and M.max() <= 1.0,
          f"range {M.min():.3f} .. {M.max():.3f}")
check("area right to about 1 percent at oversample=1", errs[1] < 3e-2,
      f"{errs[1]:.1e}")
check("oversample=4 improves it by orders of magnitude", errs[4] < 1e-4,
      f"{errs[1]:.1e} -> {errs[4]:.1e}")

big = {"period": None,
       "shapes": [{"type": "rect", "center": (0, 0), "size": (100.0, 100.0)}]}
M, x, y = mqm(big, sigma=0.0, extent=(200, 200), shape=(200, 200),
              verbose=False)
check("sigma=0 interior is fully covered",
      np.abs(M[80:120, 80:120] - 1).max() < 2e-2,
      f"max |M - 1| = {np.abs(M[80:120, 80:120] - 1).max():.2e}")
check("sigma=0 exterior is empty", np.abs(M[0:40, 0:40]).max() < 2e-2,
      f"max = {np.abs(M[0:40, 0:40]).max():.2e}")

print()
print("sub-pixel features -- the case rasterising cannot do")
# a 0.02 um disc on a 0.94 um grid: 2000x below one pixel in area
tiny = {"period": None,
        "shapes": [{"type": "circle", "center": (30.0, -20.0),
                    "diameter": 0.02}]}
M, x, y = mqm(tiny, sigma=8.0, extent=(240, 240), shape=(256, 256),
              verbose=False)
area = pi * 0.01 ** 2
got = M.sum() * (x[1] - x[0]) * (y[1] - y[0])
check("0.02 um disc keeps its exact area", abs(got - area) / area < 1e-9,
      f"{got:.6e} vs {area:.6e}")
iy, ix = np.unravel_index(np.argmax(M), M.shape)
check("...and lands within a pixel of where it belongs",
      abs(x[ix] - 30.0) <= 1.0 and abs(y[iy] + 20.0) <= 1.0,
      f"peak at ({x[ix]:.2f}, {y[iy]:.2f})")
check("...spread over the laser, not dumped in one hot pixel", M.max() < 1e-5,
      f"peak value {M.max():.2e}")
# A disc this far below a pixel is a point source of equal area, and the
# pixel average of a gaussian is again an erf difference -- so this whole
# map has an exact reference, not just its peak.
d = x[1] - x[0]
c8 = sqrt(2.0) * 8.0
gx = np.array([(erf((xi + d / 2 - 30.0) / c8)
                - erf((xi - d / 2 - 30.0) / c8)) / (2 * d) for xi in x])
gy = np.array([(erf((yi + d / 2 + 20.0) / c8)
                - erf((yi - d / 2 + 20.0) / c8)) / (2 * d) for yi in y])
ref = area * np.outer(gy, gx)
rel = np.abs(M - ref).max() / ref.max()
check("...and matches a point source of equal area, everywhere", rel < 1e-5,
      f"max |diff| / peak = {rel:.2e}")

print()
print("overlapping shapes add, and are clipped back to a coverage fraction")
# two identical discs on top of each other would sum to 2 -- the Fourier
# method is linear and cannot take a union
twice = {"period": None,
         "shapes": [{"type": "circle", "center": (0, 0), "diameter": 40.0},
                    {"type": "circle", "center": (0, 0), "diameter": 40.0}]}
M, x, y = mqm(twice, sigma=3.0, extent=(160, 160), shape=(160, 160),
              verbose=False)
check("a doubled disc is clipped to 1, not left at 2", M.max() <= 1.0,
      f"max {M.max():.4f}")
once = {"period": None,
        "shapes": [{"type": "circle", "center": (0, 0), "diameter": 40.0}]}
M1, _, _ = mqm(once, sigma=3.0, extent=(160, 160), shape=(160, 160),
               verbose=False)
# Well inside the shape the clip reproduces the union exactly. It differs
# only within a few sigma of the edge, where the true union has a
# penumbra and the doubled sum saturates early -- the price of linearity.
check("...and deep in the interior it matches the single disc",
      np.abs(M[76:84, 76:84] - M1[76:84, 76:84]).max() < 1e-5,
      f"max |diff| = {np.abs(M[76:84, 76:84] - M1[76:84, 76:84]).max():.2e}")
# The difference is confined to the penumbra: at the edge itself the true
# coverage is 0.5, so a doubled sum saturates there and differs by 0.5.
# More than 4 sigma either side of the edge the two agree.
XX, YY = np.meshgrid(x, y)
R = np.sqrt(XX ** 2 + YY ** 2)
far = np.abs(R - 20.0) > 4 * 3.0
check("...and differs only within a few sigma of the edge",
      np.abs(M - M1)[far].max() < 2e-4,
      f"max |diff| away from the edge = {np.abs(M - M1)[far].max():.2e}")
check("invert of a clipped map stays in [0, 1]",
      mqm(twice, sigma=3.0, extent=(160, 160), shape=(160, 160),
          invert=True, verbose=False)[0].min() >= 0.0)

print()
print("periodic tiling")
per = {"period": (100.0, 100.0),
       "shapes": [{"type": "circle", "center": (0, 0), "diameter": 20.0}]}
M, x, y = mqm(per, sigma=6.0, extent=(300, 300), shape=(300, 300),
              verbose=False)
e = max(np.abs(M[:, 0:100] - M[:, 100:200]).max(),
        np.abs(M[:, 100:200] - M[:, 200:300]).max(),
        np.abs(M[0:100, :] - M[100:200, :]).max())
check("output repeats with the lattice period", e < 1e-9,
      f"max |diff| = {e:.2e}")
# the window spans -150..150, so copies sit at x = -100, 0, 100
rows = [int(round((xc + 150.0) / (x[1] - x[0]) - 0.5)) for xc in (-100, 0, 100)]
peaks = [M[150, i] for i in rows]
check("every column of copies is present", min(peaks) > 0.7,
      "peaks " + ", ".join(f"{v:.3f}" for v in peaks))
# and the gaps between them are dark
check("the gaps between copies stay dark", M[150, rows[0] // 2] < 0.01,
      f"{M[150, rows[0] // 2]:.4f}")
check("mean coverage equals the cell fill fraction",
      abs(M.mean() - pi * 100 / 100 ** 2) < 1e-6,
      f"{M.mean():.6f} vs {pi * 100 / 100 ** 2:.6f}")
M2, _, _ = mqm(per, sigma=6.0, extent=(300, 300), shape=(300, 300),
               center=(5000.0, -3000.0), verbose=False)
check("the same lattice is seen 5 mm away", np.abs(M - M2).max() < 1e-9,
      f"max |diff| = {np.abs(M - M2).max():.2e}")

print()
print("huge period with sub-micron features (the real CNF case)")
t0 = time.time()
M, x, y = mqm(patterns.SID, sigma=3.0, extent=(220, 220), shape=(300, 300),
              verbose=False)
dt = time.time() - t0
check("1 cm period / 0.02 um features is finite and fast",
      np.isfinite(M).all() and dt < 5.0, f"{dt * 1e3:.0f} ms")

print()
print("options")
tiled = {"period": (200.0, 200.0),
         "shapes": [{"type": "rect", "center": (0, 0), "size": (100.0, 100.0)}]}
base, _, _ = mqm(tiled, sigma=5.0, extent=(220, 220), shape=(128, 128),
                 verbose=False)
inv, _, _ = mqm(tiled, sigma=5.0, extent=(220, 220), shape=(128, 128),
                invert=True, verbose=False)
check("invert complements the map", np.abs((base + inv) - 1).max() < 1e-9,
      f"max |M + M_inv - 1| = {np.abs((base + inv) - 1).max():.2e}")
scaled, _, _ = mqm(tiled, sigma=5.0, extent=(220, 220), shape=(128, 128),
                   qe_range=(0.2, 0.8), verbose=False)
check("qe_range rescales into [lo, hi]",
      np.abs(scaled - (0.2 + 0.6 * base)).max() < 1e-9,
      f"max |diff| = {np.abs(scaled - (0.2 + 0.6 * base)).max():.2e}")
n1, _, _ = mqm(tiled, sigma=5.0, extent=(220, 220), shape=(128, 128),
               noise=0.05, rng=np.random.default_rng(0), verbose=False)
n2, _, _ = mqm(tiled, sigma=5.0, extent=(220, 220), shape=(128, 128),
               noise=0.05, rng=np.random.default_rng(0), verbose=False)
check("noise is reproducible from a seeded rng", np.array_equal(n1, n2))
# noise is FRACTIONAL: 5% of the local value, so it vanishes where the
# mask is dark rather than adding a uniform floor
lit = base > 0.5
frac = np.std((n1 - base)[lit] / base[lit])
check("noise is the requested fraction of the local value",
      0.045 < frac < 0.055, f"rms fraction {frac:.4f}")
check("noise does not light up the dark areas",
      np.abs(n1[base < 1e-3]).max() < 1e-3,
      f"max in dark {np.abs(n1[base < 1e-3]).max():.2e}")

_, xa, ya = mqm(tiled, sigma=5.0, extent=(220, 110), shape=(64, 32),
                verbose=False)
check("axes match extent and shape",
      len(xa) == 64 and len(ya) == 32
      and abs((xa[-1] - xa[0]) - (220 - 220 / 64)) < 1e-9
      and abs((ya[-1] - ya[0]) - (110 - 110 / 32)) < 1e-9,
      f"x {xa[0]:.3f}..{xa[-1]:.3f}, y {ya[0]:.3f}..{ya[-1]:.3f}")

print()
print("the example masks (ppgun1 has the triangles MATLAB refused to blur)")
for nm, pat in patterns.ALL.items():
    try:
        M, x, y = mqm(pat, sigma=12.0, extent=(220, 220), shape=(200, 200),
                      verbose=False)
        ok = np.isfinite(M).all() and M.min() > -1e-6 and M.max() < 1.0 + 1e-6
        check(nm, ok, f"range {M.min():.3f} .. {M.max():.3f}")
    except Exception as exc:                        # noqa: BLE001
        check(nm, False, f"{type(exc).__name__}: {exc}")

tri = {"period": None,
       "shapes": [{"type": "polygon", "center": (0.0, 0.0),
                   "points": [(0.0, 0.0), (60.0, 25.0), (60.0, -25.0)]}]}
M, x, y = mqm(tri, sigma=4.0, extent=(200, 200), shape=(200, 200),
              verbose=False)
check("a lone triangle blurs, and stays symmetric in y",
      np.abs(M - M[::-1, :]).max() < 1e-9,
      f"max |M - flip(M)| = {np.abs(M - M[::-1, :]).max():.2e}")
tri_cw = {"period": None,
          "shapes": [{"type": "polygon", "center": (0.0, 0.0),
                      "points": [(0.0, 0.0), (60.0, -25.0), (60.0, 25.0)]}]}
Mcw, _, _ = mqm(tri_cw, sigma=4.0, extent=(200, 200), shape=(200, 200),
                verbose=False)
check("polygon winding order does not matter",
      np.abs(M - Mcw).max() < 1e-9, f"max |diff| = {np.abs(M - Mcw).max():.2e}")

if failures:
    print(f"\n{len(failures)} FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("\nall model_qe_map checks passed")
