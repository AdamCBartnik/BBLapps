# VPCam Wiring Reference

## CM5 Nano B GPIO Header

The LED controller PCBA connects to the CM5 via the GPIO header. The three signals used are an inline grouping on the right column of the header.

| Signal | CM5 GPIO (BCM) | Header Pin | Notes |
|---|---|---|---|
| 5V Power | — | Pin 4 | Powers LED strip via transistor |
| GND | — | Pin 6 | Emitter ground |
| LED Control | BCM 18 | Pin 12 | NPN low-side switch output |

> BCM 18 (pin 12) is the PCM_CLK pin. No conflicts with UART or other required peripherals — no additional setup required.

---

## LED Controller Circuit

```
CM5 GPIO18 ──── 4.7kΩ (1206) ──── Base (MMBT2222A SOT-23)
                                   Collector ──── LED Strip (–)
                                   Emitter  ──── GND (Pin 6)

LED Strip (+) ──── 5V (Pin 4)
```

- **Transistor:** MMBT2222A (SOT-23 footprint)
- **Base resistor:** 4.7kΩ (1206)
- **LED strip current:** ~20–60mA @ 5V
- **Logic level:** 3.3V CM5 GPIO drives base resistor directly

> ⚠️ `led_status` PV reflects commanded software state only — not hardware confirmation. If the transistor or LED fails, the PV will still read 1.

---

## Camera (CSI)

Camera Module 3 connects to the CM5 Nano B via the onboard 15-pin CSI-2 ribbon connector. Use **CAM1** (not CAM0) on the Waveshare Nano B baseboard.

---

## Enclosure Connector Summary

| Connection | Connector Type | Notes |
|---|---|---|
| Ethernet | RJ45 passthrough | Lab network |
| Power | USB-C (5V/3A min) | Via Nano B USB-C port |
| GPIO to protoboard | JST-XH 3-pin | Wires soldered to Nano B GPIO header pins; JST connector on protoboard |
| LED to protoboard | JST-XH 2-pin | Wires soldered to LED leads; JST connector on protoboard |
| Camera | Internal CSI ribbon | Inside enclosure |

---

### JST Connector Orientation

The three GPIO signals (5V, GND, LED control) break out from the Nano B's GPIO header to the LED controller protoboard via a JST-XH 3-pin connector. The wires are soldered directly to the GPIO header pins on the Nano B side; the mating JST-XH plug is on the protoboard.

The LED connects to the protoboard through a 2-pin JST-XH connector. The wires are soldered to the LED leads; the mating JST-XH plug is on the protoboard.

---

