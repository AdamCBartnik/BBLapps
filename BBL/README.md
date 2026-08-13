# BBL

Shared Python utilities for **Cornell's Bright Beams Lab (BBL)**. Much of this 
is ported from the group's original MATLAB scripts and is very much a work in progress.

## Install

```
pip install -e .          # from the repo root
```

Requires numpy, matplotlib, pyepics, and ipympl. Nothing is imported until you touch it, 
so `import BBL` stays fast and a missing optional dependency only breaks the subpackage that needs it.

## Layout

Organised into subpackages

| Subpackage | Contents |
|---|---|
| `bbl.epics` | `caget`, `caput`, `restore_pvs` |
| `bbl.plot` | `LivePlot`, `warmup`, `get_colormap` |
| `bbl.image` | `get_frame`, `plot_frame`, `screen_sensitivity_correction` |
| `bbl.utilities` | `ssss`, `next_ssss_stem`, `get_todays_directory`, `polyfit_weights`, `measure_trend` |
| `bbl.gun` | `center_laser_in_gun`, `fit_gun_aberration` |
| `bbl.solenoid` | `solenoid_scan`, `fit_solenoid_scan` |
| `bbl.fieldmaps` | `load_onaxis_field` |
| `bbl.cnf` | `model_qe_map`, `patterns` |

---

## An example scan script

Most hand-written scans look like this — set something, measure something,
watch it happen, fit it, save it.

```python
import time
import numpy as np
import BBL as bbl

%matplotlib widget
bbl.plot.warmup()          # once per kernel, before the first live plot

setpoints = np.linspace(-0.5, -5.0, 10)

lp = bbl.plot.LivePlot(xlabel="solenoid current (A)", ylabel="centroid (um)")
lp.set_interactive(False)      # don't let stray clicks queue up during the scan

done, cx, cx_err, cy, cy_err = [], [], [], [], []
try:
    with bbl.epics.restore_pvs("SOL1_cmd"):        # put it back, whatever happens
        for current in setpoints:
            bbl.epics.caput("SOL1_cmd", current)
            time.sleep(0.5) # Allow solenoid to settle

            x, xe = bbl.epics.caget("B24:centroid_x", n_avg=10, stale=True)
            y, ye = bbl.epics.caget("B24:centroid_y", n_avg=10, stale=True)

            done.append(current)
            cx.append(x); cx_err.append(xe)
            cy.append(y); cy_err.append(ye)

            lp.update(done, cx, y_err=cx_err, label="x", style="ro")
            lp.update(done, cy, y_err=cy_err, label="y", style="bs")
finally:
    lp.set_interactive(True)   # give the mouse back even if the scan threw

# fit, draw it on the same axes, and force the redraw ourselves
coeffs, errs, cov = bbl.utilities.polyfit_weights(done, cx, cx_err, deg=1)
lp.ax.plot(done, np.polynomial.polynomial.polyval(np.array(done), coeffs), "k-")
lp.refresh()
print(f"slope = {coeffs[1]:.3f} +/- {errs[1]:.3f} um/A")

bbl.utilities.ssss(lp.fig, name="solenoid_scan",
                   data={"current": np.array(done),
                         "centroid_x": np.array(cx),
                         "centroid_y": np.array(cy)})
```

Some details in there:

- **`set_interactive` is paired with a `try`/`finally`.** If the scan raises, or
  you interrupt it, the plot would otherwise stay frozen to the mouse for the
  rest of the session.
- **`restore_pvs` wraps the loop**, so the solenoid goes back to where it
  started on a normal finish, an exception, or a Ctrl-C.
- **`stale=True` on the reads**, because a `caput` was just issued and the
  cached centroid might still be from the previous setpoint. 
- **`refresh()` after drawing the fit**, since that artist was added directly
  to the axes rather than through `update`, which would have redrawn for us.

---

# `bbl.epics`

### `bbl.epics.caget(pv_names, n_avg=1, pause=0.0, max_pause=5.0, stale=False, return_std=False)`

Read one or more PVs through the monitor cache. **Never raises** — an
unreachable PV comes back as NaN, so a long scan doesn't die on one bad
channel. A sequence of names returns an array.

| Argument | Meaning |
|---|---|
| `n_avg` | Samples to average. `>1` returns `(avg, std)`. |
| `pause` | `0` (default) uses camonitor updates. `>0` samples every `pause` seconds instead. |
| `max_pause` | With camonitor pacing, give up and return NaN if a PV goes this long without updating. |
| `stale` | Treat the cached value as stale, so a fresh update is required. E.g. use after a `caput`. |
| `return_std` | Force the `(avg, std)` return even when `n_avg == 1`. |

### `bbl.epics.caput(pv_names, values, wait=True, timeout=5.0)`

Write one or more PVs. `wait=True` blocks until the IOC confirms the record
processed; `wait=False` fires and returns. A scalar
value broadcasts across a sequence of names. Returns `True` if every put
landed — failures are printed, not raised.

### `bbl.epics.restore_pvs(*pv_names)`

Context manager that records PVs on entry and writes them back on exit. 
The restore runs on **any** exit, including an
exception or a Ctrl-C / kernel interrupt.

```python
with bbl.epics.restore_pvs("MA1CHA01_cmd", "MA1CVA01_cmd"):
    ...  # scan
```

Unlike `caget`/`caput`, this one **does** raise if a PV won't connect —
silently scanning something you can't put back would be worse than stopping.


---

# `bbl.plot`

### `bbl.plot.warmup()`

This is a workaround for a jupyterlab annoyance. Run once in the top cell of
a notebook right after `%matplotlib widget`. The first widget in a
fresh kernel needs a frontend handshake that can't complete while a scan is
blocking the kernel — so without this, the first live plot of a session stays
invisible until its scan finishes. 

### `bbl.plot.LivePlot(xlabel='', ylabel='', title='', ax=None, style='ro', capsize=3)`

A matplotlib error-bar plot that updates in place while a scan runs.

| Argument | Meaning |
|---|---|
| `xlabel`, `ylabel`, `title` | Set on the axes if given. |
| `ax` | Draw into an existing Axes. By default it makes its own figure and displays the widget immediately, rather than waiting for the cell to end — which is what lets you watch a scan as it goes. |
| `style` | Default matplotlib format string for traces, e.g. `'ro'`. |
| `capsize` | Error-bar cap size. |

Two traces on one axes, each identified by its own `label` and told apart by
its `style`:

```python
lp = bbl.plot.LivePlot(xlabel="solenoid current (A)", ylabel="centroid (um)")
...
lp.update(done, cx, y_err=cx_err, label="x", style="ro")
lp.update(done, cy, y_err=cy_err, label="y", style="bs")
```

Both `update` calls grow the same plot rather than fighting over it, because
the labels differ. Give the same label twice and the second call replaces the
first. Note that `label` is LivePlot's own key for the trace and is not passed
to matplotlib, so it will not appear in `ax.legend()` — `style` is what
distinguishes the traces on screen.

### `bbl.plot.LivePlot.update(x, y, y_err=None, label=None, style=None)`

Replace this trace's data and redraw. **Pass the full arrays every time** —
`update` replaces what is drawn rather than appending to it, so the usual
pattern is to grow a list and hand over the whole thing each iteration.

| Argument | Meaning |
|---|---|
| `x`, `y` | The complete data so far. |
| `y_err` | Optional error bars. Give it and you get an errorbar plot, omit it and you get a plain line. |
| `label` | Which trace to replace. Different labels are independent traces on the same axes, so several quantities can share one plot. `None` is itself a valid label — the default single trace. |
| `style` | Format string for this trace. Remembered per trace, so you only need it the first time. |

The artist is rebuilt each call, because a matplotlib `ErrorbarContainer`
can't have its data swapped in place. That is cheap at scan cadence. Calls
`refresh()` for you, so a plain `update` loop needs nothing else.

### `bbl.plot.LivePlot.refresh()`

Force a redraw now, from inside a blocking loop. `update` already calls it —
you only need it directly after changing the axes yourself, say adding a fit
line or an axvline mid-scan.

It issues a synchronous `draw()` rather than `draw_idle()` on purpose. Under
ipympl, `draw_idle` needs a kernel↔browser round trip that cannot complete
while a cell is blocked in a scan loop, so nothing would appear until the scan
finished — exactly when you no longer need it. `draw()` renders in the kernel
and pushes the frame straight to the browser.

Autoscaling is applied only when it is still on, so a toolbar zoom or pan
survives subsequent updates instead of being yanked back.

### `bbl.plot.LivePlot.set_interactive(enabled=True)`

Freeze or unfreeze mouse interaction with the figure.

Touching a live plot while a scan holds the kernel — zooming, clicking, even
just hovering — queues mouse events browser-side. They compete with the frame
updates during the scan, then replay all at once as chaos when the cell ends.
`set_interactive(False)` sets `pointer-events: none` on the canvas so the
browser sends nothing at all, and `set_interactive(True)` gives it back.

No-op outside Jupyter, so it is safe to leave in a script.

### `bbl.plot.get_colormap(name=None, m=256, p=1.0, return_list=False)`

The lab's colormaps from a variety of sources, many from cmasher. Append `_r` to any name to reverse it.

| Call | Returns |
|---|---|
| `get_colormap()` | Sorted list of the available names |
| `get_colormap("freeze")` | A matplotlib `Colormap`, ready to pass to `cmap=` |
| `get_colormap("freeze", return_list=True)` | The raw `(m, 3)` float RGB array |

`m` resamples to that many entries, `p` applies a power-law to the resampling.
`return_list=True` is for consumers that build their own lookup table


---

# `bbl.image` — camera frames

### `bbl.image.get_frame(name, units='physical', timeout=5.0)`

Grab the current frame from a camera IOC, or load a saved snapshot.

`name` is either a camera's own EPICS areaDetector prefix as used in
beamview's config (e.g. `"B24Screen1"`) — **not** beamview's "To EPICS"
publish prefix, which is a different namespace — or the path to a beamview
`ssss` snapshot `.h5`, which is loaded without touching EPICS at all.

`units='physical'` (default) gives `xx`/`yy` in the camera's calibrated unit
via `cam1:CalibX/_Y`, falling back to pixels with a printed note if the camera
isn't calibrated. `'pixels'` forces pixels.

Returns a dict: `image`, `xx`, `yy`, `title`, `camera_name`, `exposure_ms`,
`gain`, `colormap`, `cmap_reversed`, `display_min`, `display_max`, `bits`,
`width`, `height`, `roi`, `unique_id`, `timestamp`. Raises `RuntimeError` if
no frame is available.

### `bbl.image.plot_frame(data, ax=None, log=False, show_colorbar=True, cmap=None, vmin=None, vmax=None, title=None)`

Plot a `get_frame()` dict with matplotlib. Returns the Axes.

| Argument | Meaning |
|---|---|
| `log` | Show `log10(1 + abs(image))`. Colour range auto-scales to the transformed data, since the stored display range describes the raw image. |
| `cmap` | Override the colormap name (default: the frame's own, else `freeze`). Falls back to gray if the name is unknown. |
| `vmin`/`vmax` | Override the display range (default: the frame's stored one). |
| `show_colorbar` | On by default. |

### `bbl.image.screen_sensitivity_correction(frame, sensitivity, sigma=1.0, threshold=None, floor=0.25, erode=9, fill=nan, return_details=False)`

Divide out a viewscreen's non-uniform response. `frame` and `sensitivity` are
`get_frame()` dicts or plain 2-D arrays, and must be the same shape — same
camera, same hardware ROI. The `sensitivity` map is what you get from sweeping
the beam over the screen with beamview's *Save max value*.

| Argument | Meaning |
|---|---|
| `sigma` | Gaussian blur on the map before dividing, in pixels. Suppresses pixel-to-pixel noise in the max. **Keep it small** — a bigger blur looks smoother while correcting less. |
| `threshold` | Sensitivity value separating swept from unswept. `None` picks it from the histogram (Otsu). |
| `floor` | Smallest permitted gain, as a fraction of the median — caps how hard a dim pixel can be amplified. |
| `erode` | Shrink the valid region by this many pixels to drop the partially-swept rim. `0` disables. |
| `fill` | Written where the map has no coverage. NaN by default, so "not measured" stays obvious. |
| `return_details` | Also return the mask and gain actually used. |

Returns a corrected copy of `frame` — a dict if you passed one, so
`plot_frame` and `ssss` work on the result directly.


---

# `bbl.utilities`

### `bbl.utilities.ssss(fig=None, name='ssss', data=None, directory=None, dpi=150)`

"See something, save something" — writes a figure into today's data directory.

| Argument | Meaning |
|---|---|
| `fig` | A Figure or Axes, or `None` for the current figure. Axes is accepted so `bbl.utilities.ssss(bbl.image.plot_frame(frame))` works. |
| `name` | Filename prefix. The default `ssss` is always numbered (`ssss_001`, …); any other prefix is used bare first, then `_2`, `_3`, … |
| `data` | Optional dict written alongside as `<stem>.h5`. Pass a `get_frame()` dict and `get_frame()` can read it back. |
| `directory` | Override the destination. |

Returns the `Path` of the PNG.

### `bbl.utilities.next_ssss_stem(directory, prefix='ssss', reserve=())`

The stem-picking behind `ssss`, exposed for anything that writes its own
files. `reserve` is a list of extensions to create as empty placeholders
before returning, so a concurrent saver can't claim the same number.

### `bbl.utilities.get_todays_directory(day=None, n_relative_day=0)`

The lab's dated data directory as a `Path` —
`…/beamdata/YYYY/MM/YYYY-MM-DD`, rooted at `\\samba\bbl_online\beamdata` on
Windows and `/nfs/bbl/online/beamdata` otherwise.

`day` accepts `None` (today), a negative int (days back), a `date`/`datetime`,
or a date string. `n_relative_day` offsets from today. Future dates raise.

### `bbl.utilities.polyfit_weights(x, y, y_err=None, deg=1)`

Weighted polynomial fit, a port of `polyfitweights.m`. Returns
`(coeffs, coeff_errs, cov)` with coefficients **lowest power first** — the
`numpy.polynomial` convention, i.e. the reverse of `np.polyval` and MATLAB.

Errors are absolute-sigma: `y_err` is taken as the true measurement error and
the covariance is *not* rescaled by the residuals, matching MATLAB. With no
`y_err` the fit is unweighted and the returned errors are zero. Non-finite
points — such as a scan point where `caget` bailed out — are dropped first.

### `bbl.utilities.measure_trend(cmd_pv, setpoints, monitor_pvs, n_avg=15, cmd_pause=0.0, pause=0.0, max_pause=5.0, poly_deg=1, plot=True, stale=True, verbose=False)`

Scan one PV over `setpoints` and measure how other PVs respond. `cmd_pv` is
restored at the end, including on Ctrl-C.

| Argument | Meaning |
|---|---|
| `monitor_pvs` | Readbacks to plot and fit; one name or a sequence. |
| `n_avg` | Averages per setpoint. |
| `cmd_pause` | Wait after each command before measuring. |
| `pause`, `max_pause`, `stale` | Passed through to `caget` — see there. |
| `poly_deg` | Polynomial order to fit. `None` skips the fit. |
| `plot`, `verbose` | Live plot, and per-setpoint progress. |

Returns a dict with `setpoints`, `avg`/`std` of shape `(n_points, n_monitors)`,
`fits` as `{pv: (coeffs, coeff_errs)}`, and the `LivePlot` objects.


---

# `bbl.gun` — gun electrical centre

### `bbl.gun.center_laser_in_gun(pvs, scan_range=7.0, num_points=11, n_avg=2, calib_h=-0.044, calib_v=0.056, calib_kv=350.0, …)`

Raster the laser spot across the cathode, recentring the beam on the screen at
each point, to find the gun's electrical centre. Returns a data dict; refit it
any time with `fit_gun_aberration`.

`pvs` is a dict of PV names with keys `laser_h_cmd`/`_rdbk`,
`laser_v_cmd`/`_rdbk`, `corr_h_cmd`/`_rdbk`, `corr_v_cmd`/`_rdbk`, `sol_cmd`,
`centroid_x`/`_y` (beamview-published, in any consistent screen unit), and
optionally `gun_volt` for momentum-scaling the corrector calibration.

| Argument | Meaning |
|---|---|
| `scan_range` | Full span per axis, mm on the cathode. |
| `num_points` | Grid size per axis (`num_points²` points total). |
| `calib_h`, `calib_v` | Corrector calibration, amps per screen unit, measured at `calib_kv`. **Signs matter** — a wrong sign makes the recentring loop diverge, and it aborts after `max_recenter_iter`. |
| `laser_pos_accuracy` | Laser stage move tolerance, mm. |
| `screen_pos_accuracy` | Recentring convergence tolerance, in `centroid_x/_y`'s unit. |

Timeouts, pauses and iteration caps have sensible defaults — see the docstring.

### `bbl.gun.fit_gun_aberration(data, beta0=None, verbose=True)`

Fit the cubic-aberration model to a `center_laser_in_gun` result. Returns
`params`, `errs`, `cov`, and `model_pos`.

The electrical centre `xc`/`yc`/`theta` comes out in `laser_pos`'s unit (mm,
the cathode side) regardless of what unit `beam_pos` is in — a uniform
rescaling of `beam_pos` is absorbed by the other parameters.


---

# `bbl.solenoid` — solenoid scan

### `bbl.solenoid.solenoid_scan(pvs, current_setpoints, fieldmap, drift_length, n_avg=10, …)`

Scan the solenoid, track the beam centroid's spiral, and fit it for the beam's
position and angle at the solenoid entrance. Returns a data dict, already
fitted; refit with `fit_solenoid_scan(data, fieldmap, drift_length)`.

`pvs` keys:

| Key | Meaning |
|---|---|
| `sol_cmd`/`_rdbk` | Solenoid current, A. |
| `screen` | beamview's **publish** prefix (e.g. `"B24"`) — `centroid_x`, `centroid_y` and `peak_intensity` are read beneath it. |
| `laser_power_cmd` | Optional. Enables the automatic intensity servo. |
| `camera` | Required if `laser_power_cmd` is given: the camera's own areaDetector prefix (e.g. `"B24Screen1"`), used to read the bit depth. A different namespace from `screen`, so it can't be derived. |
| `gun_volt` | Optional kV readback, for computing momentum / Bρ. |

| Argument | Meaning |
|---|---|
| `current_setpoints` | Currents to scan, A — any order or sign. |
| `n_avg` | Centroid frames averaged per setpoint. |
| `intensity_min_frac`/`max_frac` | Target `peak_intensity` band as a fraction of full scale. Only used with `laser_power_cmd`. |
| `degauss`, `degauss_current` | Pulse the solenoid ± then to zero before scanning, to remove hysteresis. |

### `bbl.solenoid.fit_solenoid_scan(data, fieldmap, drift_length, current_scale=1.0, brho=None, verbose=True)`

Returns `params`/`errs` for `x_off`, `xp_off`, `y_off`, `yp_off`, plus `cov`
and the fitted `model_x`/`model_y`. Offsets come out in `centroid_x/_y`'s unit
and angles in that unit per metre.

> **`drift_length` must be accurate.** It is the surveyed distance in **metres**
> from the *solenoid centre* to the screen. The fitted parameters depend
> strongly on it — a post-solenoid drift trades off against beam angle, so a
> shorter drift with a bigger angle traces almost the same spiral as a longer
> drift with a smaller one. Fit *quality* barely changes, so **the residual
> will not warn you.** Supply the surveyed value; never tune it to the fit.

The solenoid centre is found as the Bz²-weighted centroid of the field map,
not the midpoint, so an asymmetric map is handled correctly.


---

# `bbl.fieldmaps`

### `bbl.fieldmaps.load_onaxis_field(gdf_path)`

Load an on-axis Bz(z) map, in tesla per amp, from a `.gdf` file. Handles both
1-D maps and 2-D (r, z) maps, taking the on-axis slice in the latter case.
Returns `(z, bz)` sorted by z on a uniform grid — feed it straight to
`solenoid_scan`/`fit_solenoid_scan` as `fieldmap`.

The `.gdf` maps themselves live in this directory but are gitignored.


---

# `bbl.cnf` — photocathode masks

### `bbl.cnf.model_qe_map(pattern, sigma=0.0, extent=(220., 220.), shape=(300, 300), center=(0., 0.), laser_angle=0.0, qe_range=(0., 1.), invert=False, noise=0.0, oversample=1, pad_sigmas=5.0, verbose=True, rng=None)`

Model a lithographically patterned photocathode as a gaussian laser spot sees
it — the pattern convolved with the spot. Returns `(M, x, y)` with `M` shaped
`(ny, nx)`, indexed `M[iy, ix]`.

This is a Fourier method: every primitive has a closed-form transform, so
**nothing is ever rasterised** and sub-pixel features stay exact. A 0.02 µm dot
on a 3.75 µm grid carries precisely its own area instead of occupying a whole
pixel or vanishing depending on where the grid happens to fall. The FFT covers
the output window rather than the lattice cell, so cost is independent of the
period — a 1 cm cell viewed through a 220 µm window costs the same as a 220 µm
cell.

`pattern` is a dict of `"shapes"` and an optional `"period"`:

```python
{"period": (2000.0, 2000.0),          # lattice, or None for a one-off layout
 "shapes": [
     {"type": "rect",    "center": (0, 0), "size": (w, h), "angle": 0},
     {"type": "circle",  "center": (0, 0), "diameter": d},
     {"type": "ellipse", "center": (0, 0), "size": (a, b), "angle": 0},
     {"type": "polygon", "center": (0, 0), "points": [(x, y), ...], "angle": 0},
 ]}
```

Patterns are **data, not built in** — pass your own, or start from
`bbl.cnf.patterns`, which holds the five ported MATLAB masks (`DENSE`,
`LESS_DENSE`, `SID`, `PPGUN1`, `PPGUN2`, and `ALL` keyed by name).

| Argument | Meaning |
|---|---|
| `sigma` | Laser rms size. A scalar, or `(sigma_u, sigma_v)` for an elliptical spot. **`0` works** and gives the fractional pixel coverage of the bare layout. |
| `laser_angle` | Orientation of `sigma_u` relative to +x, degrees. |
| `extent` | Output window size in pattern units, `(wx, wy)`. |
| `shape` | Output window in pixels, `(nx, ny)`. Note the returned array is `(ny, nx)`. |
| `center` | Where the window sits in the pattern. |
| `qe_range` | Rescale the 0–1 coverage onto `(min, max)`. |
| `invert` | Swap covered and uncovered. |
| `noise` | Fractional gaussian noise, e.g. `0.02` for 2% rms. Scales with the local value. |
| `oversample` | Compute on an n× finer grid and average down. Only worth raising when `sigma` is at or below a pixel — above that it buys nothing for n² the work. |
| `pad_sigmas` | Window padding, in sigma, that stops the blur wrapping. |

The filled area fraction is **printed**, not returned (`verbose=False` to
silence it).

Two things worth knowing:

- **Resolution is set by σ, not by feature size.** Sub-pixel features are exact
  regardless, so `shape` only needs to resolve the blur — a few pixels per σ.
  The exception is σ = 0, where `shape` alone sets how finely you see the
  layout, and `oversample` earns its keep.
- **Overlapping shapes add, they do not merge** — a union isn't a linear
  operation, so the Fourier method can't take one. Masks are normally
  non-overlapping, so in practice this only catches mistakes; the result is
  clipped back to `[0, 1]` and says so when it happens.

Validated against closed forms rather than itself — a gaussian-blurred
rectangle has an exact elementary answer, and so does its average over a pixel:

```
python BBL/cnf/test_qe_map.py
```
