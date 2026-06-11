"""
vpcam_extensions.py — metadata for the VPCam extension PV surface.

These specs declare WHICH extension PVs a VPCam camera serves under cam1:
(names, types, docs, poll periods) without binding any behavior.  They are
the single source of truth for two consumers:

  - vpcam_drivers.py (the Pi camera drivers) attaches hardware accessors
    to these specs via dataclasses.replace()
  - gateway_ioc.py rebinds them to CA relay calls when mirroring a VPCam
    source (--extensions common / imx708)

Getters/setters are deliberately None here — a spec with no accessors serves
as a static PV, which is never what you want; always bind before use.
"""

from ad_ioc_base import ExtensionPV

COMMON_EXTENSION_SPECS = [
    ExtensionPV(name='LedEnable', dtype=int, initial=0,
                doc='Illumination LED on/off'),
    ExtensionPV(name='LedStatus_RBV', dtype=int, initial=0, read_only=True,
                doc='LED commanded-state readback (software state, not '
                    'hardware feedback)',
                poll_period=1.0),
    ExtensionPV(name='AeEnable', dtype=int, initial=0,
                doc='Auto exposure: 0=manual, 1=auto. Must be 0 for '
                    'AcquireTime/Gain writes to take effect'),
    ExtensionPV(name='HFlip', dtype=int, initial=0,
                doc='Horizontal flip (may reconfigure camera, ~1 s)'),
    ExtensionPV(name='VFlip', dtype=int, initial=0,
                doc='Vertical flip (may reconfigure camera, ~1 s)'),
    ExtensionPV(name='Hostname_RBV', dtype=str, initial='', read_only=True,
                doc='Device hostname'),
    ExtensionPV(name='IpAddr_RBV', dtype=str, initial='', read_only=True,
                doc='Device IP (primary outbound interface)',
                poll_period=30.0),
    ExtensionPV(name='Uptime_RBV', dtype=float, initial=0.0, read_only=True,
                doc='IOC uptime (s)', poll_period=5.0),
    ExtensionPV(name='CpuTemp_RBV', dtype=float, initial=0.0, read_only=True,
                doc='CPU temperature (C)', poll_period=10.0),
]

IMX708_EXTENSION_SPECS = [
    ExtensionPV(name='SensorMode', dtype=int, initial=0,
                doc='0=Full(4608x2592), 1=2x2Binned(2304x1296); '
                    'reconfigures camera and resets ROI'),
    ExtensionPV(name='AfMode', dtype=int, initial=0,
                doc='Autofocus mode (0=Manual, 1=Auto, 2=Continuous)'),
    ExtensionPV(name='LensPosition', dtype=float, initial=0.0,
                doc='Lens position (diopters); AfMode must be 0'),
    ExtensionPV(name='Brightness', dtype=float, initial=0.0,
                doc='Brightness offset (-1.0..1.0)'),
    ExtensionPV(name='Contrast', dtype=float, initial=1.0,
                doc='Contrast multiplier (0..32)'),
    ExtensionPV(name='Sharpness', dtype=float, initial=1.0,
                doc='Sharpness (0..16, 0=disabled)'),
    ExtensionPV(name='NoiseReductionMode', dtype=int, initial=0,
                doc='0=Off, 1=Fast, 2=HighQuality'),
]
