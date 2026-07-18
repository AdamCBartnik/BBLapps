"""
demo_ioc.py — tiny caproto IOC for testing BBL scan tools with no hardware.

Serves three PVs (default prefix TEST:):

    TEST:cmd      command value, writable
    TEST:linear   readback:  2.0 * cmd + 1.0   + noise
    TEST:quad     readback:  0.5 * (cmd - 1)^2 + noise

Both readbacks repost at 10 Hz with a little Gaussian noise, so they
behave like beamview stats (continuous camonitor updates) — the right
test bed for caget's fresh-update veto and measure_trend's live plot.

Run it:
    python -m BBL.demo_ioc            (or  python BBL/demo_ioc.py)

Then, in a notebook:
    %matplotlib widget
    import numpy as np, BBL as bbl
    data = bbl.measure_trend('TEST:cmd', np.linspace(-2, 2, 11),
                             ['TEST:linear', 'TEST:quad'], n_avg=5)
    # expect: slope = 2 +/- small on TEST:linear

If the client can't find the PVs, set (before importing epics):
    EPICS_CA_ADDR_LIST=localhost  and  EPICS_CA_AUTO_ADDR_LIST=NO
"""
import numpy as np
from caproto.server import PVGroup, ioc_arg_parser, pvproperty, run

RATE_HZ = 5.0
NOISE = 0.1          # 1-sigma noise on both readbacks

LIN_SLOPE = 2.0
LIN_OFFSET = 1.0
QUAD_CURV = 0.5       # quad = QUAD_CURV * (cmd - QUAD_X0)**2
QUAD_X0 = 1.0


class DemoIOC(PVGroup):
    """cmd plus a linear and a quadratic readback, reposted with noise."""

    cmd = pvproperty(value=0.0, dtype=float, doc="command value")
    linear = pvproperty(value=LIN_OFFSET, dtype=float, read_only=True,
                        doc=f"{LIN_SLOPE}*cmd + {LIN_OFFSET} + noise")
    quad = pvproperty(value=QUAD_CURV * QUAD_X0 ** 2, dtype=float,
                      read_only=True,
                      doc=f"{QUAD_CURV}*(cmd - {QUAD_X0})^2 + noise")

    @linear.scan(period=1.0 / RATE_HZ)
    async def linear(self, instance, async_lib):
        c = float(self.cmd.value)
        rng = np.random.default_rng()
        await self.linear.write(
            LIN_SLOPE * c + LIN_OFFSET + rng.normal(0.0, NOISE))
        await self.quad.write(
            QUAD_CURV * (c - QUAD_X0) ** 2 + rng.normal(0.0, NOISE))


def main():
    ioc_options, run_options = ioc_arg_parser(
        default_prefix="TEST:",
        desc="BBL demo IOC: cmd + linear/quad readbacks with noise")
    ioc = DemoIOC(**ioc_options)
    print("[demo_ioc] serving:", ", ".join(ioc.pvdb))
    run(ioc.pvdb, **run_options)


if __name__ == "__main__":
    main()
