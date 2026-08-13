r"""
model_qe_map() -- a lithographic photocathode pattern as a laser would see it.

The pattern is a list of shapes, optionally tiled on a lattice. The laser
is a (possibly elliptical, possibly rotated) gaussian. The answer is their
convolution, sampled on a grid.

    import BBL as bbl
    from BBL.cnf import patterns

    M, x, y = bbl.cnf.model_qe_map(patterns.DENSE, sigma=15.0,
                                   extent=(220, 220), shape=(300, 300))

WHY NOT JUST RASTERISE AND BLUR
-------------------------------
Because the features can be far smaller than a pixel. A 0.02 um dot on a
3.75 um grid either lands on a pixel (becoming 3.75 um wide) or misses and
vanishes entirely -- and which one depends on sub-pixel placement, so the
answer flickers as you nudge the grid. Real masks here run to 1 cm lattice
spacing with sub-micron features, where resolving a feature would need a
40000^2 grid.

HOW THIS WORKS INSTEAD
----------------------
Nothing is ever rasterised. Convolution is a product in Fourier space, and
every primitive has a closed-form transform, so:

    1. enumerate which lattice copies of which shapes land near the window
    2. sum their analytic transforms on the window's k-grid
    3. multiply by the transfer function (below)
    4. one inverse FFT

A 0.02 um disc is then simply a small flat contribution of amplitude
pi*R^2 -- exactly right, at any grid spacing.

The FFT is over the OUTPUT WINDOW, not the lattice cell. That matters: it
makes the cost independent of the period, so a 1 cm cell viewed through a
220 um window costs the same as a 220 um cell.

An FFT domain is a torus, so the padding has to cover everything that
could come back around the far side: several sigma of gaussian tail AND
the largest shape's own extent, since a copy centred just outside the
window still reaches into it. Copies are enumerated against the output
window rather than the padded domain, so nothing is included that could
only ever wrap.

SHAPES ADD, THEY DO NOT MERGE
-----------------------------
Superposition is what makes all of the above work, and it is also the one
real limitation: two shapes that overlap contribute twice there, because
a union is not a linear operation. Masks here are non-overlapping, so in
practice this only catches mistakes. The result is clipped back into
[0, 1] -- exact at sigma = 0, and off only within a few sigma of an edge
otherwise -- and says so when it happens.

THE TRANSFER FUNCTION
---------------------
    H(k) = exp(-(su^2 ku^2 + sv^2 kv^2)/2)  *  sinc(kx dx/2) sinc(ky dy/2)
           gaussian, in the beam's own frame    the pixel's own box

The sinc is not a fudge -- it is the pixel integrating over its own area,
which is what a detector does. It earns its place twice over:

  * It supplies roll-off at Nyquist even when sigma supplies none, so
    sigma = 0 stays meaningful instead of aliasing into nonsense.
  * At sigma = 0 the result IS the fractional area of each pixel covered
    by the shapes. A sub-pixel dot appears at its true area fraction
    rather than vanishing or being smeared to a whole pixel.

So there is no minimum sigma and no second code path for the unblurred
case. Below sigma ~ dx you are looking at the pixel-averaged layout rather
than a resolved blur, which is what you want when checking a mask; the
function says so once rather than refusing.

The one honest caveat: a box filter rolls off as 1/k where a gaussian
rolls off exponentially, so at sigma = 0 there is residual aliasing --
percent-level ringing at hard edges. Fine for looking at a layout, not
something to measure against. `oversample=2` or more cleans it up by
computing on a finer k-grid and averaging down (4 is worth ~500x).

HOW WELL IT WORKS
-----------------
A gaussian-blurred rectangle has an exact elementary answer, and so does
its average over a pixel, so there is a reference to check against rather
than just self-consistency. test_qe_map.py does that: agreement is at
1e-14, and blurred shapes conserve their area to 1e-13 at any sigma.
"""
import numpy as np


# --------------------------------------------------------------------------
# Analytic Fourier transforms, with the convention
#     F(k) = integral over the shape of exp(-i k.r) d2r
# so that F(0) is the shape's area.
# --------------------------------------------------------------------------

def _sinc(u):
    """sin(u)/u, and 1 at u=0. numpy's sinc is sin(pi x)/(pi x)."""
    return np.sinc(u / np.pi)


def _rotate(kx, ky, deg):
    if not deg:
        return kx, ky
    t = np.radians(deg)
    c, s = np.cos(t), np.sin(t)
    # rotating the SHAPE by +t is rotating k by -t
    return c * kx + s * ky, -s * kx + c * ky


def _ft_rect(kx, ky, w, h, angle):
    ku, kv = _rotate(kx, ky, angle)
    return w * h * _sinc(ku * w / 2.0) * _sinc(kv * h / 2.0)


def _ft_ellipse(kx, ky, a, b, angle):
    """Semi-axes a, b. A disc is a == b."""
    from scipy.special import j1
    ku, kv = _rotate(kx, ky, angle)
    # An ellipse is a scaled disc; scaling r by (a,b) scales k by (1/a,1/b)
    q = np.sqrt((a * ku) ** 2 + (b * kv) ** 2)
    out = np.full(q.shape, np.pi * a * b, dtype=float)
    nz = q > 1e-12
    out[nz] = 2.0 * np.pi * a * b * j1(q[nz]) / q[nz]
    return out


def _ft_polygon(kx, ky, pts):
    """Any simple polygon, via the divergence theorem.

    exp(-ik.r) = (i/|k|^2) div( k exp(-ik.r) ), so the area integral turns
    into a loop over edges; each edge integrates to a sinc. This is what
    makes triangles work -- there is no elementary real-space form for a
    gaussian-blurred triangle, but its transform is elementary.
    """
    p = np.asarray(pts, dtype=float)
    if p.shape[0] < 3:
        raise ValueError("a polygon needs at least 3 points")
    q = np.roll(p, -1, axis=0)                      # next vertex

    # The edge sum below assumes counter-clockwise winding -- it uses the
    # OUTWARD normal, which flips with the winding, negating the whole
    # transform. Rather than demand a convention of the caller, detect it
    # from the signed (shoelace) area and reverse a clockwise polygon.
    area = 0.5 * np.sum(p[:, 0] * q[:, 1] - q[:, 0] * p[:, 1])
    if area < 0:
        p = p[::-1]
        q = np.roll(p, -1, axis=0)
        area = -area

    e = q - p                                       # edge vectors
    mid = 0.5 * (p + q)

    k2 = kx * kx + ky * ky
    out = np.zeros(kx.shape, dtype=complex)
    for (ex, ey), (mx, my) in zip(e, mid):
        # (k . outward normal) * edge length, for a CCW polygon
        kn = kx * ey - ky * ex
        phase = np.exp(-1j * (kx * mx + ky * my))
        out += kn * phase * _sinc(0.5 * (kx * ex + ky * ey))
    with np.errstate(invalid="ignore", divide="ignore"):
        out = 1j * out / k2
    # k = 0 is the area, and the 0/0 above left it undefined
    out[k2 == 0] = area
    return out


def _shape_ft(shape, kx, ky):
    """Transform of one shape, centred at the origin, plus its area."""
    t = str(shape["type"]).lower()
    ang = float(shape.get("angle", 0.0))
    if t in ("rect", "rectangle", "r"):
        w, h = shape["size"]
        return _ft_rect(kx, ky, float(w), float(h), ang), float(w) * float(h)
    if t in ("circle", "c", "disc", "disk"):
        # diameter, matching the MATLAB shape lists
        r = 0.5 * float(shape["diameter"])
        return _ft_ellipse(kx, ky, r, r, 0.0), np.pi * r * r
    if t in ("ellipse", "e"):
        a, b = (0.5 * float(v) for v in shape["size"])
        return _ft_ellipse(kx, ky, a, b, ang), np.pi * a * b
    if t in ("polygon", "poly", "p", "triangle", "t"):
        pts = np.asarray(shape["points"], dtype=float)
        if ang:
            th = np.radians(ang)
            c, s = np.cos(th), np.sin(th)
            pts = pts @ np.array([[c, -s], [s, c]]).T
        ft = _ft_polygon(kx, ky, pts)
        qq = np.roll(pts, -1, axis=0)
        area = abs(0.5 * np.sum(pts[:, 0] * qq[:, 1] - qq[:, 0] * pts[:, 1]))
        return ft, area
    raise ValueError(f"unknown shape type {shape['type']!r}")


def _shape_radius(shape):
    """Rough bounding radius about the shape's centre, for tiling."""
    t = str(shape["type"]).lower()
    if t in ("rect", "rectangle", "r"):
        w, h = shape["size"]
        return 0.5 * float(np.hypot(w, h))
    if t in ("circle", "c", "disc", "disk"):
        return 0.5 * float(shape["diameter"])
    if t in ("ellipse", "e"):
        return 0.5 * max(float(v) for v in shape["size"])
    pts = np.asarray(shape["points"], dtype=float)
    return float(np.max(np.hypot(pts[:, 0], pts[:, 1])))


# --------------------------------------------------------------------------

def model_qe_map(pattern, sigma=0.0, extent=(220.0, 220.0), shape=(300, 300),
                 center=(0.0, 0.0), laser_angle=0.0, qe_range=(0.0, 1.0),
                 invert=False, noise=0.0, oversample=1, pad_sigmas=5.0,
                 verbose=True, rng=None):
    """Convolve a photocathode pattern with a gaussian laser spot.

    pattern: dict with
        "shapes": list of shape dicts. Each has "type" and "center", plus
            rect/ellipse: "size" (full width, full height) and optional
                          "angle" in degrees
            circle:       "diameter"   (diameter, as in the MATLAB masks)
            polygon:      "points", a list of (x, y), and optional "angle"
        "period": (px, py) to tile the shapes on a lattice, or None/absent
            for a one-off layout.
    sigma: laser rms size. A scalar, or (sigma_u, sigma_v) for an elliptical
        spot. 0 gives the pixel-averaged layout (see the module docstring).
    laser_angle: degrees, orientation of sigma_u relative to +x.
    extent, shape, center: the output window -- size in pattern units,
        (nx, ny) pixels, and where it sits.
    qe_range: (min, max) the 0..1 coverage is scaled onto.
    invert: swap covered and uncovered.
    noise: fractional gaussian noise, e.g. 0.02 for 2% rms.
    oversample: compute on this much finer a grid and average down. Only
        worth raising when sigma is at or below a pixel and the edge
        ringing bothers you.
    pad_sigmas: window padding, in sigma, that stops the blur wrapping.

    Returns (M, x, y) with M shaped (ny, nx), matching M[iy, ix].
    """
    shapes = list(pattern.get("shapes", ()))
    if not shapes:
        raise ValueError("pattern has no 'shapes'")
    period = pattern.get("period")

    sx, sy = (float(sigma), float(sigma)) if np.isscalar(sigma) else \
             (float(sigma[0]), float(sigma[1]))
    if sx < 0 or sy < 0:
        raise ValueError("sigma must be >= 0")

    nx, ny = int(shape[0]), int(shape[1])
    wx, wy = float(extent[0]), float(extent[1])
    cx, cy = float(center[0]), float(center[1])
    dx, dy = wx / nx, wy / ny

    os_ = max(1, int(oversample))
    fdx, fdy = dx / os_, dy / os_

    # ---- padded FFT domain -------------------------------------------
    # The FFT domain is a torus, so anything that runs off one edge comes
    # back in the other. Two things must not wrap into the output window:
    # the gaussian tail (a few sigma), and the shapes themselves -- a copy
    # centred just outside the window still has its own extent. Hence the
    # pad carries both. With a pad of R + B, a copy is enumerated only if
    # its centre is within wx/2 + R + B, so its far edge reaches at most
    # wx/2 + 2(R + B) = wx/2 + 2*pad; requiring Lx >= wx + 2*pad puts the
    # wrapped remainder past the far side of the window, where it cannot
    # be seen.
    blur_pad = max(pad_sigmas * max(sx, sy), 4 * max(dx, dy))
    max_radius = max([_shape_radius(sh) for sh in shapes], default=0.0)
    pad = blur_pad + max_radius
    Nx = int(2 ** np.ceil(np.log2((wx + 2 * pad) / fdx)))
    Ny = int(2 ** np.ceil(np.log2((wy + 2 * pad) / fdy)))
    Lx, Ly = Nx * fdx, Ny * fdy

    kx = 2 * np.pi * np.fft.fftfreq(Nx, d=fdx)
    ky = 2 * np.pi * np.fft.fftfreq(Ny, d=fdy)
    KX, KY = np.meshgrid(kx, ky, indexing="xy")     # (Ny, Nx)

    # ---- which lattice copies can reach the output window? ------------
    # Measured against the OUTPUT window, not the padded domain: a copy
    # further out than this cannot influence any output pixel, and
    # enumerating it anyway would only let it wrap back in (see above).
    half_x, half_y = 0.5 * wx, 0.5 * wy
    total_area = 0.0
    F = np.zeros(KX.shape, dtype=complex)

    for sh in shapes:
        ft0, area = _shape_ft(sh, KX, KY)
        total_area += area
        reach = _shape_radius(sh) + blur_pad
        ox, oy = float(sh["center"][0]), float(sh["center"][1])

        if period:
            px, py = float(period[0]), float(period[1])
            # copies whose CENTRE lies within reach of the window: ceil on
            # the low end and floor on the high end, so the range is the
            # lattice sites inside the interval. floor/ceil would instead
            # add a spurious cell at each end.
            i0 = int(np.ceil((cx - half_x - reach - ox) / px))
            i1 = int(np.floor((cx + half_x + reach - ox) / px))
            j0 = int(np.ceil((cy - half_y - reach - oy) / py))
            j1 = int(np.floor((cy + half_y + reach - oy) / py))
            offsets = [(ox + i * px, oy + j * py)
                       for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)]
        else:
            offsets = [(ox, oy)]

        for gx, gy in offsets:
            # position relative to the window centre, then a phase shift
            F += ft0 * np.exp(-1j * (KX * (gx - cx) + KY * (gy - cy)))

    # ---- transfer function: laser gaussian, then the pixel's own box ---
    KU, KV = _rotate(KX, KY, laser_angle)
    H = np.exp(-0.5 * ((sx * KU) ** 2 + (sy * KV) ** 2))
    H = H * _sinc(KX * fdx / 2.0) * _sinc(KY * fdy / 2.0)
    # Half-pixel shift: the FFT samples the domain at 0, fdx, 2fdx, ... i.e.
    # on pixel EDGES, while a pixel average belongs at the pixel CENTRE --
    # which is also where the returned x/y say the samples are. Without this
    # the whole map sits half a pixel off, worth (dx/2)*|grad| ~ a few
    # percent on a blurred edge.
    H = H * np.exp(0.5j * (KX * fdx + KY * fdy))

    field = np.real(np.fft.ifft2(F * H)) / (fdx * fdy)

    # ---- crop the window out of the padded domain ---------------------
    # the window is centred on the domain, which is centred on `center`
    ix0 = int(round((Lx / 2 - wx / 2) / fdx))
    iy0 = int(round((Ly / 2 - wy / 2) / fdy))
    field = np.roll(field, (Ny // 2, Nx // 2), axis=(0, 1))
    M = field[iy0:iy0 + ny * os_, ix0:ix0 + nx * os_]

    if os_ > 1:                       # average the fine grid back down
        M = M.reshape(ny, os_, nx, os_).mean(axis=(1, 3))

    # ---- present -------------------------------------------------------
    # A coverage fraction lives in [0, 1]. Two things can push it outside:
    # the sigma=0 ringing (a few percent, see the module docstring), and
    # shapes that overlap -- the transforms are SUMMED, so a hole punched
    # twice counts twice. The Fourier method cannot take a union, since a
    # union is not linear, so overlapping shapes are simply not allowed;
    # clipping gives the right answer for the layout and the warning says
    # where to look. The threshold sits above the ringing so a plain
    # sigma=0 map does not trip it.
    overlap = float(M.max())
    M = np.clip(M, 0.0, 1.0)
    if verbose and overlap > 1.1:
        print(f"[model_qe_map] coverage reached {overlap:.3g} before "
              f"clipping: shapes in this pattern overlap, and overlapping "
              f"shapes add rather than merge. Clipped to 1.")
    if invert:
        M = 1.0 - M
    lo, hi = float(qe_range[0]), float(qe_range[1])
    M = lo + (hi - lo) * M
    if noise:
        g = np.random.default_rng() if rng is None else rng
        M = M * g.normal(1.0, float(noise), M.shape)

    x = cx + (np.arange(nx) + 0.5) * dx - 0.5 * wx
    y = cy + (np.arange(ny) + 0.5) * dy - 0.5 * wy

    if verbose:
        if period:
            frac = total_area / (float(period[0]) * float(period[1]))
            print(f"[model_qe_map] filled area fraction: {frac:.6g} "
                  f"({total_area:.4g} per {float(period[0]):.6g} x "
                  f"{float(period[1]):.6g} cell)")
        else:
            print(f"[model_qe_map] total shape area: {total_area:.6g}")
        if max(sx, sy) < max(dx, dy):
            print(f"[model_qe_map] sigma ({max(sx, sy):.3g}) is below the "
                  f"pixel size ({max(dx, dy):.3g}): showing the "
                  f"pixel-averaged layout, not a resolved blur.")

    return M, x, y
