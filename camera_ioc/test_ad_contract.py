"""
test_ad_contract.py — end-to-end smoke test for ad_ioc_base.

Starts a real caproto server with a fake camera driver, then exercises the
standard areaDetector contract over actual Channel Access from a client
context, the same way beamview / the web UI will:

  1. read static info (MaxSizeX/Y, BitsPerPixel, Manufacturer)
  2. set exposure via cam1:AcquireTime, confirm _RBV
  3. set ROI via cam1:SizeX/SizeY, confirm clamped readbacks
  4. start acquisition (cam1:Acquire), watch image1:ArrayCounter_RBV advance
  5. counted read of image1:ArrayData (count = w*h), verify pixel values
  6. stop acquisition, verify counter stops

Run:  python test_ad_contract.py
"""

import os
import subprocess
import sys
import time

import numpy as np

PREFIX = "ADTEST:99:"
PYTHON = sys.executable

SERVER_CODE = f"""
import numpy as np
import ad_ioc_base as ad
from caproto.server import run

class FakeDriver(ad.CameraDriver):
    manufacturer = 'Test'
    model = 'FakeCam'
    extension_pvs = [
        ad.ExtensionPV(name='LedEnable', dtype=int, initial=0,
                       doc='test extension', setter=lambda d, v: int(v)),
    ]
    def __init__(self):
        self._roi = (0, 0, 64, 48)
        self._exp = 0.01
        self._gain = 1.0
        self._n = 0
    @property
    def sensor_width(self): return 64
    @property
    def sensor_height(self): return 48
    def get_roi(self): return self._roi
    def set_roi(self, x, y, w, h):
        # Clamp like real hardware
        x = max(0, min(x, 63)); y = max(0, min(y, 47))
        w = max(1, min(w, 64 - x)); h = max(1, min(h, 48 - y))
        self._roi = (x, y, w, h)
        return self._roi
    @property
    def exposure_time(self): return self._exp
    @exposure_time.setter
    def exposure_time(self, s): self._exp = float(s)
    @property
    def gain(self): return self._gain
    @gain.setter
    def gain(self, v): self._gain = float(v)
    @property
    def bits_per_pixel(self): return 10
    def capture(self):
        self._n += 1
        x, y, w, h = self._roi
        # Every pixel = frame number, so the client can verify content
        return np.full((h, w), self._n % 1024, dtype=np.uint16)

driver = FakeDriver()
IOCClass = ad.build_ioc_class(FakeDriver)
ioc = IOCClass(driver=driver, prefix='{PREFIX}')

async def startup_hook(async_lib):
    await ioc.startup()

run(ioc.pvdb, startup_hook=startup_hook,
    module_name='caproto.asyncio.server', log_pv_names=False)
"""


def main():
    env = dict(os.environ)
    env.setdefault("EPICS_CA_ADDR_LIST", "127.0.0.1")
    env.setdefault("EPICS_CA_AUTO_ADDR_LIST", "NO")

    server = subprocess.Popen(
        [PYTHON, "-u", "-c", SERVER_CODE],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        time.sleep(3.0)  # let the server come up
        if server.poll() is not None:
            print(server.stdout.read())
            raise SystemExit("server died on startup")

        from caproto.threading.client import Context
        ctx = Context()

        def pv(name):
            (p,) = ctx.get_pvs(PREFIX + name, timeout=5)
            return p

        def rd(name, **kw):
            v = pv(name).read(**kw).data
            return v[0] if len(v) == 1 else v

        failures = []

        def check(label, got, want):
            ok = (got == want)
            print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
            if not ok:
                failures.append(label)

        print("[1] static info")
        check("MaxSizeX_RBV", rd("cam1:MaxSizeX_RBV"), 64)
        check("MaxSizeY_RBV", rd("cam1:MaxSizeY_RBV"), 48)
        check("BitsPerPixel_RBV", rd("cam1:BitsPerPixel_RBV"), 10)
        check("Manufacturer_RBV", b"".join(
            rd("cam1:Manufacturer_RBV").tobytes().split(b"\\x00")).decode(),
            "Test")

        print("[2] exposure")
        pv("cam1:AcquireTime").write(0.05, wait=True)
        time.sleep(0.5)
        check("AcquireTime_RBV", round(float(rd("cam1:AcquireTime_RBV")), 6), 0.05)

        print("[3] ROI (request 200x200 at 10,10 -> expect clamp to 54x38)")
        pv("cam1:MinX").write(10, wait=True)
        pv("cam1:MinY").write(10, wait=True)
        pv("cam1:SizeX").write(200, wait=True)
        pv("cam1:SizeY").write(200, wait=True)
        time.sleep(0.5)
        check("SizeX_RBV", rd("cam1:SizeX_RBV"), 54)
        check("SizeY_RBV", rd("cam1:SizeY_RBV"), 38)
        check("MinX_RBV", rd("cam1:MinX_RBV"), 10)

        print("[4] acquisition + new-frame counter")
        pv("cam1:AcquirePeriod").write(0.1, wait=True)
        c0 = rd("image1:ArrayCounter_RBV")
        pv("cam1:Acquire").write(1, wait=True)
        time.sleep(1.2)
        c1 = rd("image1:ArrayCounter_RBV")
        print(f"  counter advanced {c0} -> {c1}")
        check("counter advanced", c1 > c0, True)

        print("[5] counted ArrayData read")
        w = int(rd("image1:ArraySize0_RBV"))
        h = int(rd("image1:ArraySize1_RBV"))
        check("ArraySize0_RBV (w)", w, 54)
        check("ArraySize1_RBV (h)", h, 38)
        data = rd("image1:ArrayData", data_count=w * h)
        arr = np.asarray(data, dtype=np.uint16)
        check("counted read size", arr.size, w * h)
        # All pixels of one fake frame are equal to the frame number
        check("frame content uniform", int(arr.min()) == int(arr.max()), True)
        check("frame content nonzero", int(arr.max()) > 0, True)

        print("[6] stop")
        pv("cam1:Acquire").write(0, wait=True)
        time.sleep(0.4)
        c2 = rd("image1:ArrayCounter_RBV")
        time.sleep(0.6)
        c3 = rd("image1:ArrayCounter_RBV")
        check("counter stopped", c3, c2)
        check("Acquire_RBV is Done", rd("cam1:Acquire_RBV"), 0)

        print()
        if failures:
            print(f"FAILED: {len(failures)} check(s): {failures}")
            raise SystemExit(1)
        print("ALL CHECKS PASSED")
    finally:
        server.terminate()
        try:
            out, _ = server.communicate(timeout=5)
            tail = "\n".join(out.splitlines()[-10:])
            print("--- server tail ---")
            print(tail)
        except Exception:
            server.kill()


if __name__ == "__main__":
    main()
