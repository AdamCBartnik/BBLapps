"""
test_ad_backend.py — beamview's EPICSAreaDetectorCamera against a VPCam
contract IOC (the mock driver), headless.

This exercises the exact client/server pair that runs at the lab:
beamview <- CA -> vpcam_launcher.py.

Run from the directory containing beamview/:  python beamview/test_ad_backend.py
(starts the mock IOC itself; requires the vpcam repo alongside, pyepics, caproto)
"""

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
VPCAM_IOC = os.path.join(ROOT, "vpcam", "ioc")

os.environ["EPICS_CA_ADDR_LIST"] = "127.0.0.1"
os.environ["EPICS_CA_AUTO_ADDR_LIST"] = "NO"

sys.path.insert(0, ROOT)

import numpy as np


def main():
    env = dict(os.environ)
    ioc = subprocess.Popen(
        [sys.executable, "-u", os.path.join(VPCAM_IOC, "mock_ioc.py"),
         "--prefix", "MOCK"],
        cwd=VPCAM_IOC, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(4)

    failures = []

    def check(label, got, want):
        ok = (got == want)
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
        if not ok:
            failures.append(label)

    try:
        if ioc.poll() is not None:
            print(ioc.stdout.read())
            raise SystemExit("mock IOC died on startup")

        from beamview.cameras.epics_areadetector import EPICSAreaDetectorCamera
        # dual_frame is declared by the caller now (no runtime probe)
        cam = EPICSAreaDetectorCamera("MOCK", dual_frame=True)
        time.sleep(1.0)

        print("[1] geometry & identity")
        check("width_max", cam.width_max, 1000)
        check("height_max", cam.height_max, 1000)
        check("bits", cam.bits, 12)
        check("max_value", cam.max_value, 4095)

        print("[2] new-frame monitor (mock auto-streams at 5 Hz)")
        cam.has_new_frame()  # clear any startup event
        time.sleep(0.5)
        check("has_new_frame fires", cam.has_new_frame(), True)
        check("flag clears after read", cam.has_new_frame(), False)

        print("[3] snapshot")
        img = cam.snapshot()
        check("shape", img.shape, (1000, 1000))
        # Native CA dtype is preserved (uint16 transports as int32 over CA;
        # a real two-image detector would serve float64). The float pipeline
        # downstream doesn't care which.
        check("integer dtype", bool(np.issubdtype(img.dtype, np.integer)), True)
        check("blob present", bool(img.max() > 300), True)

        print("[3b] dual-frame: capability + atomic pair")
        check("has_dual_frame", cam.has_dual_frame, True)
        img1, img2 = cam.snapshot_dual()
        check("image1 shape", img1.shape, (1000, 1000))
        check("image2 present", img2 is not None, True)
        check("image2 shape", img2.shape, (1000, 1000))
        # cold (image1) carries ~10% more beam than hot (image2)
        check("cold brighter than hot", bool(img1.max() >= img2.max()), True)

        print("[4] exposure set/readback")
        cam.exposure_time = 0.03
        time.sleep(0.5)
        check("exposure_time", round(cam.exposure_time, 6), 0.03)

        print("[5] hardware ROI (now actually works for AD cameras)")
        cam.set_roi(50, 40, 200, 100)
        time.sleep(0.7)
        check("get_roi", cam.get_roi(), (50, 40, 200, 100))
        time.sleep(0.7)
        img = cam.snapshot()
        check("snapshot follows ROI", img.shape, (100, 200))
        img1, img2 = cam.snapshot_dual()
        check("dual follows ROI", (img1.shape, img2.shape),
              ((100, 200), (100, 200)))
        cam.set_roi(0, 0, 1000, 1000)

        print("[6] stop/start streaming")
        cam.stop_streaming()
        time.sleep(0.5)
        cam.has_new_frame()
        time.sleep(0.8)
        check("no frames while stopped", cam.has_new_frame(), False)
        cam.start_streaming(rate_hz=5.0)
        time.sleep(1.0)
        check("frames after restart", cam.has_new_frame(), True)

        cam.close()

        print()
        if failures:
            print(f"FAILED: {len(failures)} check(s): {failures}")
            raise SystemExit(1)
        print("ALL CHECKS PASSED")
    finally:
        ioc.terminate()
        try:
            ioc.communicate(timeout=5)
        except Exception:
            ioc.kill()


if __name__ == "__main__":
    main()
