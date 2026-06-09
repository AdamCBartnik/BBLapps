"""
beamview entry point.

Usage
-----
    # With a config file (multi-camera, lab-specific):
    python -m beamview --config configs/b29.yaml

    # Quick single-camera launch (legacy / home use):
    python -m beamview --vpcam VPCAM:03:GB
    python -m beamview --mock
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
        "--vpcam", metavar="PREFIX",
        help="Launch directly with a single VPCAM camera (e.g. VPCAM:03:GB)",
    )
    group.add_argument(
        "--mock", action="store_true",
        help="Launch with the built-in mock camera (no hardware required)",
    )
    return p.parse_args()


def main():
    args = _parse_args()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    from .main_window import MainWindow

    if args.mock:
        from .cameras.mock import MockCamera
        cam = MockCamera()
        window = MainWindow(cam, lab_name="Mock", entries=None, epics_prefix="")

    elif args.vpcam:
        from .cameras.vpcam import VPCAMCamera
        cam = VPCAMCamera(args.vpcam)
        window = MainWindow(cam, lab_name=args.vpcam, entries=None, epics_prefix="")

    else:  # --config
        config_path = Path(args.config)
        if not config_path.exists():
            # Also look relative to this file's directory
            config_path = Path(__file__).parent / args.config
        try:
            from .config_loader import load_config
            lab_name, entries, epics_prefix = load_config(config_path)
        except Exception as e:
            QMessageBox.critical(None, "Config error", str(e))
            sys.exit(1)

        window = MainWindow(entries[0].camera, lab_name=lab_name,
                            entries=entries, epics_prefix=epics_prefix)

    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
