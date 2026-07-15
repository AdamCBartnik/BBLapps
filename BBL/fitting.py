"""
Weighted least-squares fitting helpers (ports of the MATLAB utilities).
"""
import numpy as np


def polyfit_weights(x, y, y_err=None, deg=1):
    """Weighted polynomial fit; port of matlab polyfitweights.m.

    Returns (coeffs, coeff_errs, cov) with coefficients LOWEST power
    first — the numpy.polynomial.polynomial.polyfit convention — so
    coeffs[0] is the constant, coeffs[1] the slope, and
    np.polynomial.polynomial.polyval(x, coeffs) evaluates the fit.
    (Note this is the reverse of np.polyval / MATLAB polyval order.)

    Errors are absolute-sigma: y_err is taken as the true measurement
    error and the covariance is NOT rescaled by the residuals, matching
    the MATLAB version.

    If y_err is None, all (effectively) zero, or negligible relative to
    y, an unweighted fit is done and the returned errors/covariance are
    zero — also matching the MATLAB version.  Nonpositive y_err entries
    are replaced with the smallest positive entry.
    """
    x = np.asarray(x, dtype=float).ravel()
    y = np.asarray(y, dtype=float).ravel()
    n_coef = deg + 1

    unweighted = y_err is None
    if not unweighted:
        y_err = np.abs(np.asarray(y_err, dtype=float).ravel())
        y_max = np.max(np.abs(y)) if y.size else 0.0
        if (y_max == 0.0 or not np.any(y_err > 0)
                or np.all(y_err < 1e-6 * y_max)):
            unweighted = True
        elif np.any(y_err <= 0):
            y_err = y_err.copy()
            y_err[y_err <= 0] = np.min(y_err[y_err > 0])

    # np.polyfit does the work (it provides the weighted covariance, which
    # numpy.polynomial's polyfit does not); flip to lowest-power-first
    if unweighted:
        coeffs = np.polyfit(x, y, deg)[::-1]
        return coeffs, np.zeros(n_coef), np.zeros((n_coef, n_coef))

    coeffs, cov = np.polyfit(x, y, deg, w=1.0 / y_err, cov="unscaled")
    coeffs = coeffs[::-1]
    cov = cov[::-1, ::-1]
    return coeffs, np.sqrt(np.diag(cov)), cov
