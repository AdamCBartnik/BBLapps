"""
test_relay_chain.py — end-to-end test of the standalone CA gateway.

Chain: MockDriver IOC (VPCAM:99) <- gateway_ioc.py (VPCAM:99:GW) <- this client.

Checks that the gateway mirrors identity/geometry, relays live frames, and
forwards ROI + exposure writes to the source.

Run:  python test_relay_chain.py   (starts both IOCs itself)
"""

import os
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable
SRC = "VPCAM:99:"
GW = "VPCAM:99:GW:"


def start_proc(argv, server_port=None, addr_list="127.0.0.1"):
    """On one host both caproto servers would bind TCP 5064 (SO_REUSEADDR on
    Windows) and searches land on the wrong server — so the gateway gets its
    own port. In production source and gateway run on different machines."""
    env = dict(os.environ)
    env["EPICS_CA_ADDR_LIST"] = addr_list
    env["EPICS_CA_AUTO_ADDR_LIST"] = "NO"
    if server_port is not None:
        env["EPICS_CA_SERVER_PORT"] = str(server_port)
    return subprocess.Popen(
        [PYTHON, "-u", *argv],
        cwd=HERE, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def main():
    # The source boots IDLE (--rate 0) and has never produced a frame:
    # ArraySize0/1_RBV are 0.  The gateway must start the source itself and
    # survive the no-frames-yet window (regression: the zero sizes used to
    # crash the relay loop fatally on first use).
    src = start_proc([os.path.join(HERE, "mock_ioc.py"), "VPCAM:99",
                      "--rate", "0"])
    time.sleep(4)
    # Gateway serves on 5074 but its internal client searches the source on
    # 5064.  --extensions none: the mock source has no LED/AE/etc. PVs, so
    # the auto prefix-sniff (VPCAM -> common) would just produce timeouts.
    gw = start_proc([os.path.join(HERE, "gateway_ioc.py"), "VPCAM:99",
                     "--extensions", "none"],
                    server_port=5074, addr_list="127.0.0.1:5064")
    time.sleep(6)

    # This client must search both ports
    os.environ["EPICS_CA_ADDR_LIST"] = "127.0.0.1:5064 127.0.0.1:5074"
    os.environ["EPICS_CA_AUTO_ADDR_LIST"] = "NO"

    failures = []
    try:
        for proc, name in [(src, "source"), (gw, "gateway")]:
            if proc.poll() is not None:
                print(proc.stdout.read())
                raise SystemExit(f"{name} IOC died on startup")

        from caproto.threading.client import Context
        ctx = Context()

        def pv(name):
            (p,) = ctx.get_pvs(name, timeout=5)
            return p

        def rd(name, **kw):
            v = pv(name).read(**kw).data
            return v[0] if len(v) == 1 else v

        def check(label, got, want):
            ok = (got == want)
            print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
            if not ok:
                failures.append(label)

        print("[1] gateway mirrors identity/geometry")
        model = bytes(np.asarray(rd(GW + "cam1:Model_RBV"),
                                 dtype=np.uint8)).decode().rstrip("\x00")
        check("model mentions gateway", "gateway" in model, True)
        check("MaxSizeX", int(rd(GW + "cam1:MaxSizeX_RBV")), 640)
        check("MaxSizeY", int(rd(GW + "cam1:MaxSizeY_RBV")), 480)
        check("BitsPerPixel", int(rd(GW + "cam1:BitsPerPixel_RBV")), 10)

        print("[2] frames flow through the gateway")
        c0 = int(rd(GW + "image1:ArrayCounter_RBV"))
        time.sleep(1.5)
        c1 = int(rd(GW + "image1:ArrayCounter_RBV"))
        print(f"  gateway counter {c0} -> {c1}")
        check("gateway counter advances", c1 > c0, True)

        w = int(rd(GW + "image1:ArraySize0_RBV"))
        h = int(rd(GW + "image1:ArraySize1_RBV"))
        img = np.asarray(rd(GW + "image1:ArrayData", data_count=w * h),
                         dtype=np.uint16)
        check("frame nonzero (blob present)", int(img.max()) > 300, True)

        print("[3] ROI write relays to source")
        pv(GW + "cam1:SizeX").write(320, wait=True)
        pv(GW + "cam1:SizeY").write(240, wait=True)
        time.sleep(1.0)
        check("source SizeX_RBV", int(rd(SRC + "cam1:SizeX_RBV")), 320)
        check("gateway SizeX_RBV", int(rd(GW + "cam1:SizeX_RBV")), 320)
        time.sleep(1.0)
        check("gateway frame width follows ROI",
              int(rd(GW + "image1:ArraySize0_RBV")), 320)

        print("[4] exposure write relays to source")
        pv(GW + "cam1:AcquireTime").write(0.025, wait=True)
        time.sleep(0.5)
        check("source AcquireTime_RBV",
              round(float(rd(SRC + "cam1:AcquireTime_RBV")), 6), 0.025)
        check("gateway AcquireTime_RBV",
              round(float(rd(GW + "cam1:AcquireTime_RBV")), 6), 0.025)

        print("[5] gateway stop does NOT stop the source")
        pv(GW + "cam1:Acquire").write(0, wait=True)
        time.sleep(0.5)
        s0 = int(rd(SRC + "image1:ArrayCounter_RBV"))
        g0 = int(rd(GW + "image1:ArrayCounter_RBV"))
        time.sleep(1.0)
        s1 = int(rd(SRC + "image1:ArrayCounter_RBV"))
        g1 = int(rd(GW + "image1:ArrayCounter_RBV"))
        check("source keeps acquiring", s1 > s0, True)
        check("gateway stopped", g1, g0)

        print("[6] relaying gateway revives a source stopped underneath it")
        pv(GW + "cam1:Acquire").write(1, wait=True)   # gateway relaying again
        time.sleep(1.0)
        pv(SRC + "cam1:Acquire").write(0, wait=True)  # kill the source directly
        time.sleep(1.0)
        check("source actually stopped", int(rd(SRC + "cam1:Acquire_RBV")), 0)
        # Starvation watchdog fires after ~STARVE_RETRIES * EVENT_WAIT seconds
        time.sleep(6.0)
        check("source revived by gateway",
              int(rd(SRC + "cam1:Acquire_RBV")), 1)
        g2 = int(rd(GW + "image1:ArrayCounter_RBV"))
        time.sleep(1.5)
        g3 = int(rd(GW + "image1:ArrayCounter_RBV"))
        check("gateway frames flowing again", g3 > g2, True)

        print()
        if failures:
            print(f"FAILED: {len(failures)} check(s): {failures}")
            raise SystemExit(1)
        print("ALL CHECKS PASSED")
    finally:
        for proc in (gw, src):
            proc.terminate()
        for proc, name in [(gw, "gateway"), (src, "source")]:
            try:
                out, _ = proc.communicate(timeout=5)
                tail = "\n".join(out.splitlines()[-6:])
                print(f"--- {name} tail ---\n{tail}")
            except Exception:
                proc.kill()


if __name__ == "__main__":
    main()
