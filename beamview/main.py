"""
beamview entry point.

Usage
-----
    # With a config file (multi-camera, lab-specific):
    python -m beamview --config configs/b29.yaml

    # Quick single-camera launch (home use):
    python -m beamview --epics VPCAM:03
    python -m beamview --mock        # connects to the mock IOC at prefix MOCK
"""

import argparse
import sys
from pathlib import Path

from PyQt5.QtWidgets import QApplication, QMessageBox


def _parse_args():
    p = argparse.ArgumentParser(description="Beamview camera GUI")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--config", metavar="FILE",
        help="YAML config file listing cameras for this lab (e.g. configs/b29.yaml)",
    )
    group.add_argument(
        "--epics", "--vpcam", dest="epics", metavar="PREFIX",
        help="Launch directly with a single standard-areaDetector IOC "
             "(e.g. VPCAM:03)",
    )
    group.add_argument(
        "--mock", action="store_true",
        help="Connect to the mock IOC at prefix MOCK (run it yourself with "
             "`python mock_ioc.py` from vpcam/ioc)",
    )
    p.add_argument(
        "--dual", action="store_true",
        help="With --epics: treat the IOC as a two-image (double) detector, "
             "enabling Hot/Cold/Diff",
    )
    return p.parse_args()


def main():
    args = _parse_args()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    from .main_window import MainWindow

    if args.mock:
        # The mock is a real IOC (prefix MOCK, served by mock_ioc.py);
        # beamview only cagets. It's a two-image camera; home-like, so the
        # per-frame "To EPICS" caput defaults off (no analysis records).
        from .cameras.epics_areadetector import EPICSAreaDetectorCamera
        cam = EPICSAreaDetectorCamera("MOCK", dual_frame=True)
        window = MainWindow(cam, lab_name="Mock", entries=None,
                            epics_prefix="", to_epics_default=False)

    elif args.epics:
        from .cameras.epics_areadetector import EPICSAreaDetectorCamera
        prefix = args.epics.rstrip(":")
        cam = EPICSAreaDetectorCamera(prefix, dual_frame=args.dual)
        window = MainWindow(cam, lab_name=prefix, entries=None, epics_prefix="")

    else:  # --config
        config_path = Path(args.config)
        if not config_path.exists():
            # Also look relative to this file's directory
            config_path = Path(__file__).parent / args.config
        try:
            from .config_loader import load_config
            lab_name, entries, epics_prefix, to_epics = load_config(config_path)
        except Exception as e:
            QMessageBox.critical(None, "Config error", str(e))
            sys.exit(1)

        window = MainWindow(entries[0].camera, lab_name=lab_name,
                            entries=entries, epics_prefix=epics_prefix,
                            to_epics_default=to_epics)

    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
