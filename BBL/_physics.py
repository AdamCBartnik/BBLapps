"""
Small shared physics helpers for the ported scan scripts.
"""
import math

#: electron rest mass, in kV (so a gun voltage in kV can be used directly
#: as an energy in keV: eV = e * V, so V[kV] numerically equals E[keV])
MC2_KV = 511.0


def momentum_from_voltage_kv(volt_kv):
    """Electron momentum (kV, i.e. keV/c) from an accelerating voltage (kV)."""
    return math.sqrt((volt_kv + MC2_KV) ** 2 - MC2_KV ** 2)


#: T*m per (kV/c) of momentum: Brho[T*m] = p[GeV/c] / 0.299792458,
#: and 1 kV/c = 1e-6 GeV/c
_BRHO_PER_KV = 1e-6 / 0.299792458


def brho_tesla_meters(momentum_kv):
    """Magnetic rigidity (T*m) for a momentum in the momentum_from_voltage_kv
    convention (kV, numerically keV/c)."""
    return momentum_kv * _BRHO_PER_KV
