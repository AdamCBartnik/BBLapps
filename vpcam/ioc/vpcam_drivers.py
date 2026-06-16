"""
vpcam_drivers.py — CameraDriver implementations for the VPCam Pi cameras.

Replaces the hardware halves of vpcam_ioc_imx708.py, vpcam_ioc_imx296.py and
vpcam_ioc_imx296_mono.py.  The PV surface is provided by ad_ioc_base; these
classes only talk to hardware.  Capture logic is ported verbatim from the
original IOCs (it is hardware-validated — be careful changing it).

Drivers:
    IMX708Driver      Camera Module 3 via picamera2: SBGGR10 raw,
                      Bayer-block-summed mono, sensor modes (full / 2x2 bin),
                      autofocus, brightness/contrast/sharpness/NR.
    IMX296Driver      Global Shutter camera via picamera2: SRGGB10 raw,
                      Bayer-block-summed mono, flips via reconfigure.
    IMX296MonoDriver  Mono Global Shutter on CM5: PiSP stores raw as
                      MONO_PISP_COMP1 which picamera2 cannot decompress, so
                      each frame is captured by rpicam-still --raw writing a
                      DNG to /dev/shm, read back with rawpy (~175 ms/frame).

(The hardware-free mock camera is a standalone tool, mock_ioc.py, not a
driver here — see its module docstring.)

All hardware libraries (picamera2, libcamera, rawpy) are imported lazily in
driver constructors so this module imports cleanly on any machine.

Config: /etc/vpcam/config.yaml (same file/keys as the original IOCs, so
deployed cameras keep their persisted settings across the upgrade).
Calibration note: config keys stay x_mm_per_pixel/y_mm_per_pixel (mm/px);
the cam1:CalibX/Y PVs are um/px — drivers convert.
"""

from __future__ import annotations

import atexit
import os
import shutil
import socket
import subprocess
import threading
import time

import numpy as np
import yaml

from ad_ioc_base import CameraDriver, ExtensionPV

CONFIG_PATH = os.environ.get('VPCAM_CONFIG', '/etc/vpcam/config.yaml')


# ---------------------------------------------------------------------------
# Shared helpers (ported from the original IOCs)
# ---------------------------------------------------------------------------

def load_config(path: str = None) -> dict:
    with open(path or CONFIG_PATH) as f:
        return yaml.safe_load(f)


def persist_config(section: str, key: str, value, path: str = None):
    """Update one key in config.yaml so the value survives a restart."""
    path = path or CONFIG_PATH
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f)
        cfg.setdefault(section, {})[key] = value
        with open(path, 'w') as f:
            yaml.dump(cfg, f)
    except Exception as e:
        print(f"[config] warning: could not persist {section}.{key}={value!r}: {e}")


def get_local_ip() -> str:
    """Primary outbound IP via the UDP trick (no packet actually sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return 'unknown'


def read_cpu_temp() -> float:
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            return int(f.read().strip()) / 1000.0
    except Exception:
        return 0.0


class PinctrlLED:
    """Drives a GPIO pin via the pinctrl CLI tool.

    Works reliably on Pi 5 / CM5 with the RP1 GPIO controller, where
    gpiozero's lgpio backend may not function under a systemd service.
    """

    def __init__(self, pin: int, initial_value: bool = False):
        self._pinctrl = shutil.which('pinctrl') or '/usr/bin/pinctrl'
        self._pin = pin
        self._value = False
        self._set(initial_value)

    def _set(self, state: bool):
        level = 'dh' if state else 'dl'
        cmd = [self._pinctrl, 'set', str(self._pin), 'op', level]
        try:
            subprocess.run(cmd, check=True, timeout=2, capture_output=True)
            self._value = state
        except FileNotFoundError:
            print(f"[LED] ERROR: pinctrl not found at '{self._pinctrl}'.")
        except subprocess.CalledProcessError as e:
            print(f"[LED] ERROR: pinctrl exit {e.returncode}; check that the "
                  "service user is in the gpio group.")
        except Exception as e:
            print(f"[LED] ERROR: {e}")

    def on(self):
        self._set(True)

    def off(self):
        self._set(False)

    @property
    def value(self) -> bool:
        return self._value

    def close(self):
        self.off()


def split_bayer_to_rgb_same_size(raw10):
    """Split a Bayer raw image into same-size R, G, B images (2x2 tile:
    B G / G R label convention; R/B swap is irrelevant for the sum)."""
    raw10 = np.asarray(raw10)
    H, W = raw10.shape
    H2 = H - (H % 2)
    W2 = W - (W % 2)
    raw = raw10[:H2, :W2]

    B = raw[0::2, 0::2]
    G1 = raw[0::2, 1::2]
    G2 = raw[1::2, 0::2]
    R = raw[1::2, 1::2]
    G = G1 + G2  # integer sum, not average

    R_full = np.repeat(np.repeat(R, 2, axis=0), 2, axis=1)
    G_full = np.repeat(np.repeat(G, 2, axis=0), 2, axis=1)
    B_full = np.repeat(np.repeat(B, 2, axis=0), 2, axis=1)

    if (H2, W2) != (H, W):
        out = []
        for plane in (R_full, G_full, B_full):
            o = np.zeros((H, W), dtype=raw10.dtype)
            o[:H2, :W2] = plane
            out.append(o)
        return tuple(out)
    return R_full, G_full, B_full


def unpack_raw_to_full16(raw0, sensor_w: int, sensor_h: int,
                         bit_depth: int) -> np.ndarray:
    """Normalize the raw-buffer layouts observed on the Pi camera stack into
    a (sensor_h, sensor_w) uint16 array of right-aligned sensor values.

    Handles:
      1. uint16 array — unpacked raw in a 16-bit container (maybe row padding)
      2. uint8 array with >= 2*width bytes/row — byte view of the same
      3. uint8 array with width elements/row — PiSP COMP1-ish fallback on the
         IMX708; 8-bit samples promoted to uint16, NOT shifted
    """
    raw = np.asarray(raw0).squeeze()
    if raw.ndim != 2:
        raise ValueError(f"Expected 2D raw after squeeze, got {raw.shape}")
    if raw.shape[0] != sensor_h:
        raise ValueError(f"Raw height {raw.shape[0]}, expected {sensor_h}")

    shift = max(0, 16 - int(bit_depth))

    if raw.dtype == np.uint8:
        useful = 2 * sensor_w
        if raw.shape[1] >= useful:
            full16 = raw[:, :useful].view('<u2').reshape(sensor_h, sensor_w)
            return (full16 >> shift).astype(np.uint16, copy=False)
        elif raw.shape[1] >= sensor_w:
            # 8-bit-per-pixel fallback: not full-depth raw, do not shift
            return raw[:, :sensor_w].astype(np.uint16, copy=False)
        raise ValueError(f"Raw row too short: {raw.shape[1]}")
    elif raw.dtype == np.uint16:
        if raw.shape[1] < sensor_w:
            raise ValueError(f"Raw row too short: {raw.shape[1]}")
        return (raw[:, :sensor_w] >> shift).astype(np.uint16, copy=False)
    raise TypeError(f"Unexpected raw dtype {raw.dtype}")


def clamp_roi(x, y, w, h, max_w, max_h):
    x = max(0, min(int(x), max_w - 1))
    y = max(0, min(int(y), max_h - 1))
    w = max(1, min(int(w), max_w - x))
    h = max(1, min(int(h), max_h - y))
    return x, y, w, h


# ---------------------------------------------------------------------------
# Extension PV accessor functions (module-level so they can be referenced in
# class-level extension_pvs lists; each receives the driver instance)
# ---------------------------------------------------------------------------

def _led_set(d, v):
    v = int(bool(v))
    (d.led.on if v else d.led.off)()
    persist_config('led', 'default_state', v, d.config_path)
    return v


def _ae_set(d, v):
    return d.set_ae_enable(int(bool(v)))


def _hflip_set(d, v):
    return d.set_flip(hflip=int(bool(v)))


def _vflip_set(d, v):
    return d.set_flip(vflip=int(bool(v)))


COMMON_EXTENSION_PVS = [
    ExtensionPV(name='LedEnable', dtype=int, initial=0,
                doc='Illumination LED on/off', setter=_led_set,
                getter=lambda d: int(d.led.value)),
    ExtensionPV(name='LedStatus_RBV', dtype=int, initial=0, read_only=True,
                doc='LED commanded-state readback (software state, not '
                    'hardware feedback)',
                getter=lambda d: int(d.led.value), poll_period=1.0),
    ExtensionPV(name='AeEnable', dtype=int, initial=0,
                doc='Auto exposure: 0=manual, 1=auto. Must be 0 for '
                    'AcquireTime/Gain writes to take effect',
                setter=_ae_set, getter=lambda d: int(d._ae_enable)),
    ExtensionPV(name='HFlip', dtype=int, initial=0,
                doc='Horizontal flip (may reconfigure camera, ~1 s)',
                setter=_hflip_set, getter=lambda d: int(d._hflip)),
    ExtensionPV(name='VFlip', dtype=int, initial=0,
                doc='Vertical flip (may reconfigure camera, ~1 s)',
                setter=_vflip_set, getter=lambda d: int(d._vflip)),
    ExtensionPV(name='Hostname_RBV', dtype=str, initial='', read_only=True,
                doc='Device hostname', getter=lambda d: socket.gethostname()),
    ExtensionPV(name='IpAddr_RBV', dtype=str, initial='', read_only=True,
                doc='Device IP (primary outbound interface)',
                getter=lambda d: get_local_ip(), poll_period=30.0),
    ExtensionPV(name='Uptime_RBV', dtype=float, initial=0.0, read_only=True,
                doc='IOC uptime (s)',
                getter=lambda d: time.time() - d._t_start, poll_period=5.0),
    ExtensionPV(name='CpuTemp_RBV', dtype=float, initial=0.0, read_only=True,
                doc='CPU temperature (C)',
                getter=lambda d: read_cpu_temp(), poll_period=10.0),
]


# ---------------------------------------------------------------------------
# Base for all VPCam Pi drivers
# ---------------------------------------------------------------------------

class PiDriverBase(CameraDriver):
    """Config handling, LED, ROI-as-software-crop, calibration persistence."""

    manufacturer = "Raspberry Pi"

    def __init__(self, config: dict, config_path: str = None):
        self.config_path = config_path or CONFIG_PATH
        self._cfg = config
        cam = config['camera']
        roi = config['roi']

        self._exposure_us = int(cam.get('exposure_time_us', 1000))
        self._gain = float(cam.get('analogue_gain', 1.0))
        self._ae_enable = int(cam.get('ae_enable', 0))
        self._hflip = int(cam.get('hflip', 0))
        self._vflip = int(cam.get('vflip', 0))
        self._t_start = time.time()
        # Serialize all camera access: the acquisition loop and extension
        # setters run on different worker threads.
        self._lock = threading.Lock()

        self.led = PinctrlLED(config['led']['gpio_pin'],
                              initial_value=bool(config['led']['default_state']))
        atexit.register(self.led.close)

        # ROI is a software crop on the full frame (clamped at capture time)
        self._roi = (int(roi['x']), int(roi['y']),
                     int(roi['width']), int(roi['height']))

    # -- ROI ------------------------------------------------------------------

    def get_roi(self):
        return clamp_roi(*self._roi, self.sensor_width, self.sensor_height)

    def set_roi(self, x, y, w, h):
        self._roi = clamp_roi(x, y, w, h, self.sensor_width, self.sensor_height)
        x, y, w, h = self._roi
        persist_config('roi', 'x', x, self.config_path)
        persist_config('roi', 'y', y, self.config_path)
        persist_config('roi', 'width', w, self.config_path)
        persist_config('roi', 'height', h, self.config_path)
        return self._roi

    # -- exposure / gain (stored; hardware applied by subclasses) --------------

    @property
    def exposure_time(self):
        return self._exposure_us / 1e6

    @exposure_time.setter
    def exposure_time(self, seconds):
        if self._ae_enable:
            print("[driver] AcquireTime write ignored — AeEnable=1")
            return
        us = max(1, int(round(seconds * 1e6)))
        self._apply_exposure(us, self._gain)
        self._exposure_us = us
        persist_config('camera', 'exposure_time_us', us, self.config_path)

    @property
    def gain(self):
        return self._gain

    @gain.setter
    def gain(self, value):
        if self._ae_enable:
            print("[driver] Gain write ignored — AeEnable=1")
            return
        v = float(value)
        self._apply_exposure(self._exposure_us, v)
        self._gain = v
        persist_config('camera', 'analogue_gain', v, self.config_path)

    def _apply_exposure(self, exposure_us: int, gain: float):
        """Push exposure/gain to hardware; overridden by picamera2 drivers."""

    def set_ae_enable(self, enable: int) -> int:
        self._ae_enable = enable
        persist_config('camera', 'ae_enable', enable, self.config_path)
        return enable

    def set_flip(self, hflip: int = None, vflip: int = None) -> int:
        if hflip is not None:
            self._hflip = hflip
            persist_config('camera', 'hflip', hflip, self.config_path)
            ret = hflip
        if vflip is not None:
            self._vflip = vflip
            persist_config('camera', 'vflip', vflip, self.config_path)
            ret = vflip
        return ret

    # -- calibration (PVs are um/px; config keys are mm/px) ---------------------

    def load_calibration(self):
        try:
            c = self._cfg['calibration']
            return (1000.0 * float(c['x_mm_per_pixel']),
                    1000.0 * float(c['y_mm_per_pixel']))
        except Exception:
            return None

    def save_calibration(self, cal_x_um, cal_y_um):
        persist_config('calibration', 'x_mm_per_pixel', cal_x_um / 1000.0,
                       self.config_path)
        persist_config('calibration', 'y_mm_per_pixel', cal_y_um / 1000.0,
                       self.config_path)


# ---------------------------------------------------------------------------
# picamera2-based drivers (IMX708 color, IMX296 color)
# ---------------------------------------------------------------------------

class Picamera2DriverBase(PiDriverBase):
    """Shared picamera2 logic: configure, raw capture, Bayer-summed mono."""

    #: raw format requested from libcamera; subclass sets this
    raw_format: str = "SBGGR10"

    def __init__(self, config: dict, size: tuple[int, int] = None, **kw):
        super().__init__(config, **kw)
        from picamera2 import Picamera2
        self._picam2 = Picamera2()
        if size is None:
            size = tuple(self._picam2.sensor_resolution)
        self._size = (int(size[0]), int(size[1]))
        self._last_metadata: dict = {}
        self._configure_and_start()

    # -- configuration -----------------------------------------------------------

    def _configure_and_start(self):
        from libcamera import Transform
        t = Transform(hflip=bool(self._hflip), vflip=bool(self._vflip))
        try:
            cfg = self._picam2.create_still_configuration(
                main={"size": self._size},
                raw={"format": self.raw_format, "size": self._size},
                transform=t)
            self._picam2.configure(cfg)
        except Exception as e:
            print(f"[driver] explicit {self.raw_format} raw config failed "
                  f"({e}); trying without explicit format")
            try:
                cfg = self._picam2.create_still_configuration(
                    main={"size": self._size}, raw={"size": self._size},
                    transform=t)
                self._picam2.configure(cfg)
            except Exception as e2:
                print(f"[driver] raw config at {self._size} failed ({e2}); "
                      "using default still config")
                self._picam2.configure(
                    self._picam2.create_still_configuration())
        self._picam2.start()
        time.sleep(2)  # let the camera settle

        # AeEnable must be set before ExposureTime/AnalogueGain so manual
        # exposure takes effect immediately instead of being overridden by AE.
        controls = {"AeEnable": bool(self._ae_enable)}
        controls.update(self._extra_initial_controls())
        if not self._ae_enable:
            controls["ExposureTime"] = self._exposure_us
            controls["AnalogueGain"] = self._gain
        self._picam2.set_controls(controls)

    def _extra_initial_controls(self) -> dict:
        return {}

    def _reconfigure(self):
        """Stop, re-apply Transform/size, restart, restore controls."""
        with self._lock:
            self._picam2.stop()
            self._configure_and_start()

    # -- hardware pushes ------------------------------------------------------------

    def _apply_exposure(self, exposure_us, gain):
        with self._lock:
            self._picam2.set_controls({"ExposureTime": int(exposure_us),
                                       "AnalogueGain": float(gain)})

    def set_ae_enable(self, enable):
        with self._lock:
            self._picam2.set_controls({"AeEnable": bool(enable)})
        super().set_ae_enable(enable)
        if not enable:
            # Sync stored exposure/gain to whatever AE settled on, so the
            # readbacks reflect hardware reality after switching to manual.
            try:
                time.sleep(0.1)
                with self._lock:
                    md = self._picam2.capture_metadata()
                exp = md.get("ExposureTime")
                g = md.get("AnalogueGain")
                if exp is not None:
                    self._exposure_us = int(exp)
                    persist_config('camera', 'exposure_time_us', int(exp),
                                   self.config_path)
                if g is not None:
                    self._gain = float(g)
                    persist_config('camera', 'analogue_gain', float(g),
                                   self.config_path)
            except Exception as e:
                print(f"[driver] AE readback failed: {e}")
        return enable

    def set_flip(self, hflip=None, vflip=None):
        ret = super().set_flip(hflip=hflip, vflip=vflip)
        self._reconfigure()
        return ret

    # -- geometry --------------------------------------------------------------------

    @property
    def sensor_width(self):
        return self._size[0]

    @property
    def sensor_height(self):
        return self._size[1]

    # -- capture (ported from capture_and_convert) --------------------------------------

    @property
    def bits_per_pixel(self):
        # Conservative single-channel saturation/linearity bound: individual
        # raw channels are 10-bit; the Bayer-block sum can exceed 1023.
        return 10

    def capture(self):
        with self._lock:
            request = self._picam2.capture_request()
            try:
                raw0 = request.make_array("raw")
                self._last_metadata = request.get_metadata()
            finally:
                request.release()
            raw_cfg = self._picam2.camera_configuration()["raw"]
            sensor_w, sensor_h = (int(v) for v in raw_cfg["size"])
            bit_depth = self._picam2.camera_configuration().get(
                "sensor", {}).get("bit_depth", 10)

        full = unpack_raw_to_full16(raw0, sensor_w, sensor_h, bit_depth)
        R, G, B = split_bayer_to_rgb_same_size(full)
        full = R + G + B  # Bayer-block-summed mono

        x, y, w, h = clamp_roi(*self._roi, full.shape[1], full.shape[0])
        return np.ascontiguousarray(full[y:y + h, x:x + w])

    def close(self):
        try:
            self._picam2.stop()
            self._picam2.close()
        except Exception:
            pass
        self.led.close()


def _af_set(d, v):
    return d.set_af_mode(int(v))


def _lens_set(d, v):
    return d.set_lens_position(float(v))


def _sensor_mode_set(d, v):
    return d.set_sensor_mode(int(v))


def _picam_control_setter(control, lo, hi, key):
    def _set(d, v):
        clamped = max(lo, min(hi, float(v) if isinstance(lo, float) else int(v)))
        with d._lock:
            d._picam2.set_controls({control: clamped})
        persist_config('camera', key, clamped, d.config_path)
        return clamped
    return _set


class IMX708Driver(Picamera2DriverBase):
    """Camera Module 3 (IMX708): sensor modes, autofocus, image-quality
    controls.  Output is Bayer-block-summed mono in uint16."""

    model = "Camera Module 3 (IMX708)"
    raw_format = "SBGGR10"

    #: sensor mode -> frame size; None = native full resolution
    SENSOR_MODE_SIZES = {0: None, 1: (2304, 1296)}

    extension_pvs = COMMON_EXTENSION_PVS + [
        ExtensionPV(name='SensorMode', dtype=int, initial=0,
                    doc='0=Full(4608x2592), 1=2x2Binned(2304x1296); '
                        'reconfigures camera and resets ROI',
                    setter=_sensor_mode_set,
                    getter=lambda d: d._sensor_mode),
        ExtensionPV(name='AfMode', dtype=int, initial=0,
                    doc='Autofocus mode (0=Manual, 1=Auto, 2=Continuous)',
                    setter=_af_set, getter=lambda d: d._af_mode),
        ExtensionPV(name='LensPosition', dtype=float, initial=0.0,
                    doc='Lens position (diopters); AfMode must be 0',
                    setter=_lens_set, getter=lambda d: d._lens_position),
        ExtensionPV(name='Brightness', dtype=float, initial=0.0,
                    doc='Brightness offset (-1.0..1.0)',
                    setter=_picam_control_setter('Brightness', -1.0, 1.0,
                                                 'brightness')),
        ExtensionPV(name='Contrast', dtype=float, initial=1.0,
                    doc='Contrast multiplier (0..32)',
                    setter=_picam_control_setter('Contrast', 0.0, 32.0,
                                                 'contrast')),
        ExtensionPV(name='Sharpness', dtype=float, initial=1.0,
                    doc='Sharpness (0..16, 0=disabled)',
                    setter=_picam_control_setter('Sharpness', 0.0, 16.0,
                                                 'sharpness')),
        ExtensionPV(name='NoiseReductionMode', dtype=int, initial=0,
                    doc='0=Off, 1=Fast, 2=HighQuality',
                    setter=_picam_control_setter('NoiseReductionMode', 0, 2,
                                                 'noise_reduction_mode')),
    ]

    def __init__(self, config: dict, **kw):
        cam = config['camera']
        self._sensor_mode = int(cam.get('sensor_mode', 0))
        self._af_mode = int(cam.get('af_mode', 0))
        self._lens_position = float(cam.get('lens_position', 0.0))
        size = self.SENSOR_MODE_SIZES.get(self._sensor_mode)
        super().__init__(config, size=size, **kw)
        self._native_resolution = tuple(self._picam2.sensor_resolution)

    @property
    def max_frame_pixels(self):
        # ArrayData NELM must cover full-res mode even if booted binned
        return self._native_resolution[0] * self._native_resolution[1]

    def _extra_initial_controls(self):
        cam = self._cfg['camera']
        return {
            "AfMode": self._af_mode,
            "LensPosition": self._lens_position,
            "Brightness": float(cam.get('brightness', 0.0)),
            "Contrast": float(cam.get('contrast', 1.0)),
            "Sharpness": float(cam.get('sharpness', 1.0)),
            "NoiseReductionMode": int(cam.get('noise_reduction_mode', 0)),
        }

    def set_sensor_mode(self, mode: int) -> int:
        if mode not in self.SENSOR_MODE_SIZES:
            print(f"[driver] invalid sensor mode {mode}, ignored")
            return self._sensor_mode
        if mode == self._sensor_mode:
            return mode
        size = self.SENSOR_MODE_SIZES[mode]
        self._size = (tuple(self._native_resolution) if size is None
                      else (int(size[0]), int(size[1])))
        self._sensor_mode = mode
        self._reconfigure()
        # New mode invalidates the old crop — reset ROI to the new full frame
        self.set_roi(0, 0, self._size[0], self._size[1])
        persist_config('camera', 'sensor_mode', mode, self.config_path)
        return mode

    def set_af_mode(self, mode: int) -> int:
        with self._lock:
            self._picam2.set_controls({"AfMode": mode})
        self._af_mode = mode
        persist_config('camera', 'af_mode', mode, self.config_path)
        if mode == 0:
            # Sync lens position to wherever autofocus left it
            try:
                time.sleep(0.1)
                with self._lock:
                    md = self._picam2.capture_metadata()
                pos = md.get("LensPosition")
                if pos is not None:
                    self._lens_position = float(pos)
                    persist_config('camera', 'lens_position', float(pos),
                                   self.config_path)
            except Exception as e:
                print(f"[driver] lens readback failed: {e}")
        return mode

    def set_lens_position(self, pos: float) -> float:
        if self._af_mode != 0:
            print(f"[driver] LensPosition ignored — AfMode={self._af_mode}")
            return self._lens_position
        clamped = max(0.0, min(32.0, pos))
        with self._lock:
            self._picam2.set_controls({"LensPosition": clamped})
        self._lens_position = clamped
        persist_config('camera', 'lens_position', clamped, self.config_path)
        return clamped


class IMX296Driver(Picamera2DriverBase):
    """Global Shutter camera (IMX296 color), fixed 1456x1088."""

    model = "Global Shutter Camera (IMX296)"
    raw_format = "SRGGB10"
    SENSOR_W = 1456
    SENSOR_H = 1088

    extension_pvs = COMMON_EXTENSION_PVS

    def __init__(self, config: dict, **kw):
        super().__init__(config, size=(self.SENSOR_W, self.SENSOR_H), **kw)


# ---------------------------------------------------------------------------
# IMX296 mono via rpicam-still + rawpy
# ---------------------------------------------------------------------------

class IMX296MonoDriver(PiDriverBase):
    """Mono IMX296 on CM5: PiSP stores raw as MONO_PISP_COMP1, which
    picamera2 cannot decompress — capture goes through rpicam-still --raw
    writing a DNG to /dev/shm (tmpfs), read back with rawpy.  ~175 ms/frame
    after driver warm-up."""

    model = "Global Shutter Camera (IMX296 mono)"
    SENSOR_W = 1456
    SENSOR_H = 1088
    DNG_PATH = '/dev/shm/vpcam_frame.dng'
    JPG_PATH = '/dev/shm/vpcam_frame.jpg'

    extension_pvs = COMMON_EXTENSION_PVS

    def __init__(self, config: dict, **kw):
        super().__init__(config, **kw)
        import rawpy  # noqa: F401 — fail fast at construction if missing
        self._rawpy = rawpy

    @property
    def sensor_width(self):
        return self.SENSOR_W

    @property
    def sensor_height(self):
        return self.SENSOR_H

    @property
    def bits_per_pixel(self):
        return 10

    # exposure/gain/flips are per-capture CLI flags — no hardware push needed,
    # PiDriverBase's stored values are the source of truth.

    def set_flip(self, hflip=None, vflip=None):
        # No reconfigure needed; flags take effect on the next capture
        return PiDriverBase.set_flip(self, hflip=hflip, vflip=vflip)

    def capture(self):
        cmd = ['rpicam-still', '--immediate', '--nopreview',
               '-o', self.JPG_PATH, '--raw']
        if not self._ae_enable:
            cmd += ['--shutter', str(self._exposure_us),
                    '--gain', str(self._gain)]
        if self._hflip:
            cmd.append('--hflip')
        if self._vflip:
            cmd.append('--vflip')

        with self._lock:
            result = subprocess.run(cmd, capture_output=True, timeout=15)
            if result.returncode != 0:
                raise RuntimeError(
                    f"rpicam-still failed (exit {result.returncode}): "
                    f"{result.stderr.decode(errors='replace')}")
            with self._rawpy.imread(self.DNG_PATH) as raw:
                # 10-bit values left-aligned in 16-bit words
                full = (raw.raw_image >> 6).astype(np.uint16)

        x, y, w, h = clamp_roi(*self._roi, full.shape[1], full.shape[0])
        return np.ascontiguousarray(full[y:y + h, x:x + w])

    def close(self):
        self.led.close()


# The hardware-free mock lives in its own standalone tool, mock_ioc.py — it
# has no hardware, no config, and nothing VPCam-specific, so it is not a
# vpcam_launcher camera type.

DRIVER_MAP = {
    'imx708': IMX708Driver,
    'imx296': IMX296Driver,
    'imx296_mono': IMX296MonoDriver,
}
