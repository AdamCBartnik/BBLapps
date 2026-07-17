"""
vpcam_launcher.py — single entry point for all VPCam camera IOCs.

Replaces the old per-sensor IOCs (vpcam_ioc_imx708.py / vpcam_ioc_imx296.py /
vpcam_ioc_imx296_mono.py).  Reads camera.type from config.yaml, instantiates
the matching driver from vpcam_drivers, and serves the standard-areaDetector
PV surface defined in ad_ioc_base.  Keeps the pre-migration filename so
existing systemd services boot the new IOC without on-device changes.

Config:  /etc/vpcam/config.yaml  (override with VPCAM_CONFIG env var)
Run:     python vpcam_launcher.py --list-pvs

Startup acquisition: if camera.autotrigger_rate_hz > 0 in config.yaml, the
IOC sets AcquirePeriod = 1/rate and starts Continuous acquisition on boot
(preserves the always-streaming behavior of the original IOCs).  Set 0 to
boot idle; clients then control acquisition via cam1:Acquire.
"""

import sys

from caproto.server import ioc_arg_parser, run

from ad_ioc_base import AcquireState, ImageMode, build_ioc_class
from vpcam_drivers import DRIVER_MAP, load_config

VALID_TYPES = sorted(DRIVER_MAP)


def resolve_driver_class(config):
    """Return the driver class for camera.type.

    Only Pi-resident camera types live here.  GigE cameras and the CA
    gateway never run on a Pi — use the standalone aravis_ioc.py and
    gateway_ioc.py instead."""
    return DRIVER_MAP.get(config['camera']['type'])


def main():
    config = load_config()
    camera_type = config['camera']['type']
    prefix = config['epics']['prefix']

    driver_cls = resolve_driver_class(config)
    if driver_cls is None:
        print(f"[vpcam] unknown camera.type '{camera_type}'. "
              f"Valid: {', '.join(VALID_TYPES)}")
        sys.exit(1)

    print(f"[vpcam] starting {camera_type} IOC, prefix {prefix}:")
    driver = driver_cls(config)

    IOCClass = build_ioc_class(driver_cls)
    ioc_options, run_options = ioc_arg_parser(
        default_prefix=f"{prefix}:",
        desc=f"VPCam standard-areaDetector IOC ({camera_type})")
    ioc = IOCClass(driver=driver, **ioc_options)

    async def startup_hook(async_lib):
        await ioc.startup()
        rate = float(config['camera'].get('autotrigger_rate_hz', 0) or 0)
        if rate > 0:
            await ioc.cam1_AcquirePeriod.write(1.0 / rate)
            await ioc.cam1_ImageMode.write(ImageMode.Continuous)
            await ioc.cam1_Acquire.write(AcquireState.Acquire)
            print(f"[vpcam] continuous acquisition started at {rate} Hz")

    print("\n" + "=" * 50)
    print(f"Registered PVs — {prefix}")
    print("=" * 50)
    for name in sorted(ioc.pvdb.keys()):
        pv = ioc.pvdb[name]
        ml = (f", max_length={pv.max_length}"
              if getattr(pv, "max_length", 1) > 1 else "")
        doc = getattr(pv, "doc", "") or ""
        print(f"  {name}{ml}  # {doc}")
    print("=" * 50 + "\n")

    run(ioc.pvdb, startup_hook=startup_hook, **run_options)


if __name__ == '__main__':
    main()
