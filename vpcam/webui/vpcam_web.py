"""
vpcam_web.py
VPCam Camera System - Web UI (standard-areaDetector contract)

Serves a browser-based interface on port 8080 against the PV surface
defined in ioc/ad_ioc_base.py (cam1:/image1: standard areaDetector records
plus house extensions).

Routes:
    GET  /             — Dashboard (HTML)
    GET  /stream       — MJPEG live image stream
    GET  /snapshot     — Single JPEG frame (for save)
    GET  /api/status   — System status (JSON)
    GET  /api/config   — Read config.yaml (JSON)
    POST /api/config   — Write config.yaml (JSON body)
    GET  /api/pvs      — Read all camera PVs (JSON)
    POST /api/pvs      — Write one PV (JSON body: {pv, value})
    POST /api/action   — trigger / acquire_start / acquire_stop / roi_reset
    POST /api/roi_apply— Write MinX/MinY/SizeX/SizeY in one request
    POST /api/restart  — Restart vpcam IOC service

Run:  python vpcam_web.py
"""

import io
import os
import subprocess
import threading
import time
from collections.abc import Sequence

os.environ.setdefault('EPICS_CA_MAX_ARRAY_BYTES', '40000000')

import numpy as np
import yaml
from flask import Flask, Response, jsonify, request, send_file, stream_with_context
from PIL import Image

from caproto.sync.client import read as ca_read, write as ca_write

# caproto's sync client is not thread-safe — Flask runs with multiple threads
# and the MJPEG poller runs in a background thread, so all CA calls must be
# serialized.  Large frame reads use a separate lock so they never block
# control-path puts/gets.
_ca_lock       = threading.Lock()   # control-path: small scalar reads/writes
_ca_frame_lock = threading.Lock()   # MJPEG poller: large frame array reads

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = os.environ.get('VPCAM_CONFIG', '/etc/vpcam/config.yaml')
PORT        = int(os.environ.get('VPCAM_WEB_PORT', 8080))
STREAM_FPS       = 2
THUMB_W          = 800
THUMB_H          = 600
CA_TIMEOUT       = 2.0    # timeout for small scalar PVs
CA_FRAME_TIMEOUT = 15.0   # timeout for large image array PVs


def _load_prefix():
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
            return cfg['epics']['prefix'], cfg.get('camera', {}).get('type', 'imx296')
    except Exception:
        return 'VPCAM:01', 'imx296'


PREFIX, CAMERA_TYPE = _load_prefix()


def pv(suffix):
    return f"{PREFIX}:{suffix}"


# ---------------------------------------------------------------------------
# PV map — standard areaDetector records + house extensions (see ad_ioc_base).
# Camera-specific PVs are filtered at startup based on camera.type in config.
# ---------------------------------------------------------------------------

_IMX708_ONLY = {'cam1:AfMode', 'cam1:LensPosition', 'cam1:SensorMode',
                'cam1:Brightness', 'cam1:Contrast', 'cam1:Sharpness',
                'cam1:NoiseReductionMode'}
_PI_ONLY = {'cam1:LedEnable', 'cam1:LedStatus_RBV', 'cam1:AeEnable',
            'cam1:HFlip', 'cam1:VFlip', 'cam1:Hostname_RBV',
            'cam1:IpAddr_RBV', 'cam1:CpuTemp_RBV'}

_CAM_RW_PVS_ALL = [
    ('cam1:AcquirePeriod',      'Acquire Period (s/frame)',                'float'),
    ('cam1:AeEnable',           'Auto Exposure Enable (0=manual, 1=auto)', 'int'),
    ('cam1:AcquireTime',        'Exposure Time (s)',                       'float'),
    ('cam1:Gain',               'Analogue Gain',                           'float'),
    ('cam1:Brightness',         'Brightness (−1.0 to 1.0)',                'float'),
    ('cam1:Contrast',           'Contrast (0.0 to 32.0)',                  'float'),
    ('cam1:Sharpness',          'Sharpness (0.0 to 16.0)',                 'float'),
    ('cam1:NoiseReductionMode', 'Noise Reduction (0=Off 1=Fast 2=HQ)',     'int'),
    ('cam1:HFlip',              'Horizontal Flip (0=normal, 1=flipped)',   'int'),
    ('cam1:VFlip',              'Vertical Flip (0=normal, 1=flipped)',     'int'),
    ('cam1:MinX',               'ROI X (px)',                              'int'),
    ('cam1:MinY',               'ROI Y (px)',                              'int'),
    ('cam1:SizeX',              'ROI Width (px)',                          'int'),
    ('cam1:SizeY',              'ROI Height (px)',                         'int'),
    ('cam1:LedEnable',          'LED Enable (0=off, 1=on)',                'int'),
    ('cam1:AfMode',             'AF Mode (0=Manual 1=Auto 2=Continuous)',  'int'),
    ('cam1:LensPosition',       'Lens Position (diopters)',                'float'),
    ('cam1:SensorMode',         'Sensor Mode (0=Full, 1=Binned 2×2)',      'int'),
    ('cam1:CalibX',             'X Calibration (µm/pixel)',                'float'),
    ('cam1:CalibY',             'Y Calibration (µm/pixel)',                'float'),
]

_CAM_RO_PVS_ALL = [
    ('cam1:Model_RBV',          'Camera Model'),
    ('cam1:Acquire_RBV',        'Acquiring (0=Done, 1=Acquire)'),
    ('cam1:ArrayRate_RBV',      'Actual Frame Rate (Hz)'),
    ('cam1:MinX_RBV',           'ROI X Active (px)'),
    ('cam1:MinY_RBV',           'ROI Y Active (px)'),
    ('cam1:SizeX_RBV',          'ROI Width Active (px)'),
    ('cam1:SizeY_RBV',          'ROI Height Active (px)'),
    ('cam1:MaxSizeX_RBV',       'Sensor Width (px)'),
    ('cam1:MaxSizeY_RBV',       'Sensor Height (px)'),
    ('image1:ArraySize0_RBV',   'Frame Width (px)'),
    ('image1:ArraySize1_RBV',   'Frame Height (px)'),
    ('image1:TimeStamp_RBV',    'Last Frame Timestamp'),
    ('cam1:BitsPerPixel_RBV',   'Bits per Pixel'),
    ('cam1:LedStatus_RBV',      'LED Status (software)'),
    ('cam1:Hostname_RBV',       'Hostname'),
    ('cam1:IpAddr_RBV',         'IP Address'),
    ('cam1:Uptime_RBV',         'Uptime (s)'),
    ('cam1:CpuTemp_RBV',        'CPU Temp (°C)'),
]


def _filter_pvs(camera_type):
    drop = set()
    if camera_type != 'imx708':
        drop |= _IMX708_ONLY
    if camera_type == 'mock':
        drop |= _PI_ONLY
    rw = [e for e in _CAM_RW_PVS_ALL if e[0] not in drop]
    ro = [e for e in _CAM_RO_PVS_ALL if e[0] not in drop]
    return rw, ro


CAM_RW_PVS, CAM_RO_PVS = _filter_pvs(CAMERA_TYPE)

#: every PV suffix this camera type actually serves — guard for ca_get calls
#: outside the table routes, so we never block on nonexistent PVs
AVAILABLE_PVS = ({s for s, _, _ in CAM_RW_PVS} | {s for s, _ in CAM_RO_PVS}
                 | {'image1:ArrayCounter_RBV', 'image1:ArrayData',
                    'image1:ArraySize0_RBV', 'image1:ArraySize1_RBV',
                    'image1:TimeStamp_RBV', 'cam1:Acquire_RBV',
                    'cam1:ArrayRate_RBV', 'cam1:Uptime_RBV'})

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ca_get(suffix, timeout=CA_TIMEOUT):
    with _ca_lock:
        try:
            response = ca_read(pv(suffix), timeout=timeout)
            val = response.data

            if isinstance(val, (bytes, bytearray)):
                return val.decode('utf-8', errors='replace').rstrip('\x00'), True, None

            if hasattr(val, 'dtype'):
                if val.ndim == 0 or (hasattr(val, '__len__') and len(val) == 1):
                    return val.flat[0].item(), True, None
                # Short array where all values are printable ASCII → string PV
                if len(val) <= 256 and np.all((val >= 0) & (val <= 127)):
                    return bytes(int(v) for v in val).decode(
                        'ascii', errors='replace').rstrip('\x00').strip(), True, None
                return val.tolist(), True, None

            if isinstance(val, Sequence):
                # Enum/string PVs arrive as DbrStringArray (a UserList of
                # bytes); decode to plain JSON-safe strings
                out = [v.decode('utf-8', errors='replace').rstrip('\x00')
                       if isinstance(v, (bytes, bytearray)) else v
                       for v in val]
                return (out[0] if len(out) == 1 else out), True, None

            return val, True, None
        except TimeoutError:
            return None, False, 'CA timeout'
        except Exception as exc:
            return None, False, str(exc).split('\n')[0]


def ca_put(suffix, value, timeout=CA_TIMEOUT):
    with _ca_lock:
        try:
            ca_write(pv(suffix), [value], timeout=timeout)
            return True, None
        except TimeoutError:
            return False, 'CA timeout'
        except Exception as exc:
            return False, str(exc).split('\n')[0]


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def save_config(data):
    with open(CONFIG_PATH, 'w') as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# MJPEG stream
# ---------------------------------------------------------------------------

_frame_lock   = threading.Lock()
_last_frame   = None   # bytes — JPEG-encoded
_last_counter = None   # image1:ArrayCounter_RBV of the frame we last encoded


def _poll_camera():
    global _last_frame, _last_counter
    interval = 1.0 / STREAM_FPS
    while True:
        t0 = time.monotonic()
        try:
            # Skip the (large) ArrayData read entirely if no new frame arrived
            c_val, c_ok, _ = ca_get('image1:ArrayCounter_RBV')
            if not c_ok:
                raise ValueError("Could not read frame counter")
            if c_val == _last_counter and _last_frame is not None:
                raise StopIteration  # no new frame; just wait

            # Read dimensions so they match the frame we're about to fetch
            w_val, w_ok, _ = ca_get('image1:ArraySize0_RBV')
            h_val, h_ok, _ = ca_get('image1:ArraySize1_RBV')
            if not (w_ok and h_ok):
                raise ValueError("Could not read frame dimensions")
            w, h = int(w_val), int(h_val)
            if w <= 0 or h <= 0:
                raise ValueError(f"Invalid frame dimensions {w}x{h}")

            with _ca_frame_lock:
                response = ca_read(pv('image1:ArrayData'),
                                   data_count=w * h,
                                   timeout=CA_FRAME_TIMEOUT)
            raw = response.data
            if len(raw) < w * h:
                raise ValueError(f"Frame data too short ({len(raw)} < {w*h})")

            # Auto-scale: stretch 1st–99th percentile to 0–255 so the image
            # is visible regardless of absolute brightness.
            arr = np.asarray(raw[:w * h], dtype=np.float32).reshape(h, w)
            p_low, p_high = np.percentile(arr, (1, 99))
            if p_high > p_low:
                arr = np.clip((arr - p_low) / (p_high - p_low) * 255, 0, 255)
            img = Image.fromarray(arr.astype(np.uint8), mode='L').convert('RGB')
            img.thumbnail((THUMB_W, THUMB_H), Image.LANCZOS)

            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=80)

            with _frame_lock:
                _last_frame = buf.getvalue()
            _last_counter = c_val

        except StopIteration:
            pass
        except Exception as e:
            print(f"[cam-poll] {e}")

        elapsed = time.monotonic() - t0
        time.sleep(max(0, interval - elapsed))


def _mjpeg_generator():
    boundary = b'--vpcamframe'
    while True:
        with _frame_lock:
            frame = _last_frame
        if frame is not None:
            yield (
                boundary + b'\r\n'
                b'Content-Type: image/jpeg\r\n'
                b'Content-Length: ' + str(len(frame)).encode() + b'\r\n'
                b'\r\n' + frame + b'\r\n'
            )
        time.sleep(1.0 / STREAM_FPS)


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route('/stream')
def stream():
    return Response(
        stream_with_context(_mjpeg_generator()),
        mimetype='multipart/x-mixed-replace; boundary=vpcamframe'
    )


@app.route('/snapshot')
def snapshot():
    """Return the latest frame as a downloadable JPEG."""
    with _frame_lock:
        frame = _last_frame
    if frame is None:
        return jsonify({'ok': False, 'error': 'No frame available'}), 503
    ts = time.strftime('%Y%m%d_%H%M%S')
    return Response(
        frame,
        mimetype='image/jpeg',
        headers={'Content-Disposition': f'attachment; filename="vpcam_{ts}.jpg"'}
    )


@app.route('/api/status')
def api_status():
    def get_if_available(suffix):
        if suffix not in AVAILABLE_PVS:
            return None
        val, ok, _ = ca_get(suffix)
        return val if ok else None

    hostname_val = get_if_available('cam1:Hostname_RBV')
    ip_val       = get_if_available('cam1:IpAddr_RBV')
    uptime_val   = get_if_available('cam1:Uptime_RBV')
    temp_val     = get_if_available('cam1:CpuTemp_RBV')
    ts_val       = get_if_available('image1:TimeStamp_RBV')
    rate_val     = get_if_available('cam1:ArrayRate_RBV')
    acq_val      = get_if_available('cam1:Acquire_RBV')

    try:
        result = subprocess.run(
            ['systemctl', 'is-active', 'vpcam'],
            capture_output=True, text=True, timeout=3
        )
        ioc_status = result.stdout.strip()
    except Exception:
        ioc_status = 'unknown'

    # Acquire_RBV is an enum; it may come back as int or as the string name
    acquiring = (acq_val in (1, 'Acquire'))

    return jsonify({
        'hostname':     hostname_val,
        'ip':           ip_val,
        'uptime_s':     uptime_val,
        'cpu_temp_c':   temp_val,
        'last_frame':   ts_val,
        'frame_rate':   rate_val,
        'acquiring':    acquiring,
        'ioc_status':   ioc_status,
        'camera_type':  CAMERA_TYPE,
    })


@app.route('/api/config', methods=['GET'])
def api_config_get():
    try:
        return jsonify({'ok': True, 'config': load_config()})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/config', methods=['POST'])
def api_config_post():
    try:
        save_config(request.get_json(force=True))
        return jsonify({'ok': True})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/pvs', methods=['GET'])
def api_pvs_get():
    result = {}
    for suffix, label, _ in CAM_RW_PVS:
        val, ok, err = ca_get(suffix)
        result[suffix] = {'label': label, 'value': val, 'ok': ok, 'error': err, 'rw': True}
    for suffix, label in CAM_RO_PVS:
        val, ok, err = ca_get(suffix)
        result[suffix] = {'label': label, 'value': val, 'ok': ok, 'error': err, 'rw': False}
    return jsonify({'ok': True, 'pvs': result})


@app.route('/api/pvs', methods=['POST'])
def api_pvs_post():
    body = request.get_json(force=True, silent=True)
    if not body:
        return jsonify({'ok': False, 'error': 'Invalid JSON body'}), 400
    suffix = body.get('pv')
    value  = body.get('value')

    if suffix is None or value is None:
        return jsonify({'ok': False, 'error': 'Missing "pv" or "value"'}), 400

    pv_type = 'str'
    for s, _label, t in CAM_RW_PVS:
        if s == suffix:
            pv_type = t
            break

    try:
        if pv_type == 'int':
            value = int(value)
        elif pv_type == 'float':
            value = float(value)
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400

    ok, err = ca_put(suffix, value)
    return jsonify({'ok': ok}) if ok else (jsonify({'ok': False, 'error': err}), 500)


@app.route('/api/action', methods=['POST'])
def api_action():
    """Discrete actions: trigger, acquire_start, acquire_stop, roi_reset."""
    body = request.get_json(force=True, silent=True)
    if not body:
        return jsonify({'ok': False, 'error': 'Invalid JSON body'}), 400
    action = body.get('action')

    errors = []

    def put(suffix, val):
        ok, err = ca_put(suffix, val)
        if not ok:
            errors.append(f'{suffix}: {err}')

    if action == 'trigger':
        # Single-frame capture: Single mode, fire, then restore Continuous so
        # a later acquire_start streams as expected.
        put('cam1:ImageMode', 0)        # Single
        put('cam1:Acquire', 1)
        put('cam1:ImageMode', 2)        # Continuous
    elif action == 'acquire_start':
        put('cam1:ImageMode', 2)        # Continuous
        put('cam1:Acquire', 1)
    elif action == 'acquire_stop':
        put('cam1:Acquire', 0)
    elif action == 'roi_reset':
        w, w_ok, _ = ca_get('cam1:MaxSizeX_RBV')
        h, h_ok, _ = ca_get('cam1:MaxSizeY_RBV')
        if not (w_ok and h_ok):
            return jsonify({'ok': False, 'error': 'Could not read MaxSizeX/Y'}), 500
        put('cam1:MinX', 0)
        put('cam1:MinY', 0)
        put('cam1:SizeX', int(w))
        put('cam1:SizeY', int(h))
    else:
        return jsonify({'ok': False, 'error': f'Unknown action: {action}'}), 400

    if errors:
        return jsonify({'ok': False, 'error': '; '.join(errors)}), 500
    return jsonify({'ok': True})


@app.route('/api/roi_apply', methods=['POST'])
def api_roi_apply():
    """Write all four ROI records in one request.

    Body: {"x": int, "y": int, "w": int, "h": int}

    Standard-areaDetector ROI writes apply immediately; ordering MinX/MinY
    before SizeX/SizeY lets the driver clamp sizes against the new offsets.
    """
    body = request.get_json(force=True, silent=True)
    if not body:
        return jsonify({'ok': False, 'error': 'Invalid JSON body'}), 400
    try:
        x = int(body['x'])
        y = int(body['y'])
        w = int(body['w'])
        h = int(body['h'])
    except (KeyError, ValueError) as exc:
        return jsonify({'ok': False, 'error': f'Bad body: {exc}'}), 400

    errors = []
    for suffix, value in [
        ('cam1:MinX',  x),
        ('cam1:MinY',  y),
        ('cam1:SizeX', w),
        ('cam1:SizeY', h),
    ]:
        ok, err = ca_put(suffix, value)
        if not ok:
            errors.append(f'{suffix}: {err}')

    if errors:
        return jsonify({'ok': False, 'error': '; '.join(errors)}), 500
    return jsonify({'ok': True})


@app.route('/logo.png')
@app.route('/favicon.ico')
def logo():
    """Serve logo.png from the same directory as this script."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, 'logo.png')
    if not os.path.isfile(path):
        return '', 404
    return send_file(path, mimetype='image/png')


@app.route('/api/restart', methods=['POST'])
def api_restart():
    try:
        subprocess.run(['sudo', 'systemctl', 'restart', 'vpcam'],
                       check=True, timeout=10)
        return jsonify({'ok': True})
    except subprocess.CalledProcessError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


# ---------------------------------------------------------------------------
# Embedded HTML dashboard
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>VPCam Dashboard</title>
  <link rel="icon" type="image/png" href="/logo.png">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg:       #0f1117;
      --surface:  #1a1d27;
      --border:   #2a2e40;
      --accent:   #4f8ef7;
      --accent2:  #38c172;
      --warn:     #e3a629;
      --danger:   #e05252;
      --text:     #dde1f0;
      --muted:    #7a809e;
    }

    body { background: var(--bg); color: var(--text); font-family: system-ui, sans-serif; font-size: 14px; min-height: 100vh; }

    header { display: flex; align-items: center; gap: 14px; padding: 14px 24px; background: var(--surface); border-bottom: 1px solid var(--border); }
    header h1  { font-size: 18px; font-weight: 600; }
    .tag { margin-left: auto; font-size: 11px; color: var(--muted); background: var(--bg); border: 1px solid var(--border); border-radius: 4px; padding: 3px 8px; }

    .grid { display: grid; grid-template-columns: 360px 1fr; gap: 16px; padding: 16px; max-width: 1400px; margin: 0 auto; }

    .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
    .card-header { display: flex; align-items: center; justify-content: space-between; padding: 10px 16px; border-bottom: 1px solid var(--border); font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
    .card-body { padding: 14px 16px; }

    .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--muted); margin-right: 6px; }
    .dot.green  { background: var(--accent2); box-shadow: 0 0 6px var(--accent2); }
    .dot.yellow { background: var(--warn);    box-shadow: 0 0 6px var(--warn); }
    .dot.red    { background: var(--danger);  box-shadow: 0 0 6px var(--danger); }

    .stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .stat { background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
    .stat .label { font-size: 11px; color: var(--muted); margin-bottom: 4px; }
    .stat .value { font-size: 15px; font-weight: 600; word-break: break-all; }

    .image-pane { display: flex; flex-direction: column; }
    .image-wrap { flex: 1; display: flex; align-items: center; justify-content: center; background: #080a0f; border-radius: 0 0 10px 10px; min-height: 300px; position: relative; overflow: hidden; }
    #live-img { max-width: 100%; max-height: 60vh; object-fit: contain; display: block; }
    #stream-status { position: absolute; top: 10px; left: 10px; font-size: 11px; color: var(--muted); background: rgba(0,0,0,.55); padding: 3px 8px; border-radius: 4px; }
    .img-controls { display: flex; gap: 8px; padding: 10px 16px; border-top: 1px solid var(--border); background: var(--surface); flex-wrap: wrap; }

    .tabs { display: flex; gap: 2px; padding: 10px 16px 0; border-bottom: 1px solid var(--border); }
    .tab { padding: 7px 14px; font-size: 12px; font-weight: 500; color: var(--muted); cursor: pointer; border-radius: 6px 6px 0 0; border: 1px solid transparent; border-bottom: none; user-select: none; }
    .tab:hover { color: var(--text); }
    .tab.active { color: var(--text); background: var(--bg); border-color: var(--border); margin-bottom: -1px; }
    .tab-panels { background: var(--bg); }
    .tab-panel  { display: none; padding: 14px 16px; max-height: 55vh; overflow-y: auto; }
    .tab-panel.active { display: block; }

    .pv-table { width: 100%; border-collapse: collapse; }
    .pv-table th { text-align: left; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); padding: 4px 8px 8px; }
    .pv-table td { padding: 5px 8px; vertical-align: middle; }
    .pv-table tr + tr td { border-top: 1px solid var(--border); }
    .pv-table .pv-label { color: var(--muted); font-size: 12px; width: 55%; }
    .pv-table .pv-value { font-family: monospace; font-size: 13px; }
    .pv-table .pv-input { display: flex; gap: 6px; align-items: center; }
    .pv-table input[type=number] { width: 90px; background: var(--bg); border: 1px solid var(--border); border-radius: 5px; color: var(--text); padding: 4px 7px; font-size: 12px; font-family: monospace; }
    .pv-table input:focus { outline: none; border-color: var(--accent); }

    #config-editor { width: 100%; min-height: 380px; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; color: var(--text); font-family: monospace; font-size: 13px; padding: 12px; resize: vertical; line-height: 1.6; }
    #config-editor:focus { outline: none; border-color: var(--accent); }
    #config-msg { margin-top: 8px; font-size: 12px; min-height: 18px; }

    .btn { display: inline-flex; align-items: center; gap: 5px; padding: 6px 14px; border-radius: 6px; border: none; font-size: 13px; font-weight: 500; cursor: pointer; transition: opacity .15s; white-space: nowrap; }
    .btn:disabled { opacity: .4; cursor: not-allowed; }
    .btn:hover:not(:disabled) { opacity: .85; }
    .btn-primary { background: var(--accent); color: #fff; }
    .btn-success { background: var(--accent2); color: #fff; }
    .btn-warn    { background: var(--warn);    color: #000; }
    .btn-danger  { background: var(--danger);  color: #fff; }
    .btn-ghost   { background: transparent; border: 1px solid var(--border); color: var(--text); }
    .btn-sm { padding: 4px 10px; font-size: 12px; }

    .section-label { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; color: var(--accent); margin: 14px 0 6px; }
    .section-label:first-child { margin-top: 0; }
    .action-row { display: flex; gap: 8px; margin: 8px 0; flex-wrap: wrap; }

    .toast { position: fixed; bottom: 24px; right: 24px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 10px 16px; font-size: 13px; box-shadow: 0 4px 24px rgba(0,0,0,.4); opacity: 0; transform: translateY(10px); transition: opacity .2s, transform .2s; pointer-events: none; z-index: 99; }
    .toast.show { opacity: 1; transform: none; }
    .toast.ok  { border-color: var(--accent2); }
    .toast.err { border-color: var(--danger);  }

    #roi-canvas { position: absolute; top: 0; left: 0; width: 100%; height: 100%;
                  pointer-events: none; display: none; }
    #roi-canvas.draw-active { pointer-events: all; cursor: crosshair; display: block; }
    #roi-draw-btn.active { background: var(--accent); color: #fff; }

    .flex-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .ml-auto  { margin-left: auto; }
  </style>
</head>
<body>

<header>
  <img src="/logo.png" alt="Xelera" style="height:36px;width:auto;">
  <h1>VPCam Dashboard</h1>
  <span class="tag" id="pv-prefix-tag">loading…</span>
</header>

<div class="grid">

  <!-- Left column -->
  <div style="display:flex;flex-direction:column;gap:16px;">

    <!-- Status -->
    <div class="card">
      <div class="card-header">
        System Status
        <button class="btn btn-ghost btn-sm" onclick="refreshStatus()">↻</button>
      </div>
      <div class="card-body">
        <div class="stat-grid">
          <div class="stat"><div class="label">Hostname</div><div class="value" id="s-hostname">—</div></div>
          <div class="stat"><div class="label">IP Address</div><div class="value" id="s-ip">—</div></div>
          <div class="stat"><div class="label">Uptime</div><div class="value" id="s-uptime">—</div></div>
          <div class="stat"><div class="label">CPU Temp</div><div class="value" id="s-temp">—</div></div>
          <div class="stat">
            <div class="label">IOC Service</div>
            <div class="value"><span class="dot" id="ioc-dot"></span><span id="ioc-text">—</span></div>
          </div>
          <div class="stat"><div class="label">Last Frame</div><div class="value" id="s-frame">—</div></div>
          <div class="stat"><div class="label">Actual Rate (Hz)</div><div class="value" id="s-rate">—</div></div>
        </div>
        <div style="margin-top:12px;" class="flex-row">
          <button class="btn btn-warn btn-sm" onclick="restartIOC()">⟳ Restart IOC</button>
        </div>
      </div>
    </div>

    <!-- Controls + Config (tabbed) -->
    <div class="card" style="flex:1;">
      <div class="tabs">
        <div class="tab active" data-tab="controls" onclick="switchTab('controls')">Camera Controls</div>
        <div class="tab"       data-tab="config"   onclick="switchTab('config')">Config File</div>
      </div>
      <div class="tab-panels">

        <div class="tab-panel active" id="panel-controls">
          <div class="flex-row" style="margin-bottom:12px;">
            <button class="btn btn-primary btn-sm" onclick="refreshPVs()">↻ Read All PVs</button>
            <span class="ml-auto" style="font-size:11px;color:var(--muted);">Click ✓ to write</span>
          </div>
          <div id="pv-sections"></div>
        </div>

        <div class="tab-panel" id="panel-config">
          <div class="flex-row" style="margin-bottom:10px;">
            <button class="btn btn-ghost btn-sm" onclick="loadConfig()">↻ Reload</button>
            <button class="btn btn-success btn-sm ml-auto" onclick="saveConfig()">💾 Save to Pi</button>
          </div>
          <textarea id="config-editor" spellcheck="false"></textarea>
          <div id="config-msg"></div>
          <div style="margin-top:10px;font-size:11px;color:var(--muted);">Restart IOC after saving for changes to take effect.</div>
        </div>

      </div>
    </div>
  </div>

  <!-- Right column — image -->
  <div class="card image-pane">
    <div class="card-header">
      Live Image
      <div class="flex-row">
        <span class="dot green" id="stream-dot"></span>
        <span id="stream-label" style="font-size:11px;color:var(--muted);">connecting…</span>
      </div>
    </div>
    <div class="image-wrap">
      <span id="stream-status">Connecting to camera…</span>
      <img id="live-img" alt="Live image" style="display:none;">
      <canvas id="roi-canvas"></canvas>
    </div>
    <div class="img-controls">
      <button class="btn btn-success btn-sm" id="acquire-btn" onclick="toggleAcquire()">▶ Acquire</button>
      <button class="btn btn-primary btn-sm" onclick="doAction('trigger')">⏱ Single</button>
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;">
        <input type="checkbox" id="stream-toggle" checked onchange="toggleStream(this.checked)">
        Auto stream
      </label>
      <button class="btn btn-ghost btn-sm" id="roi-draw-btn" onclick="toggleROIDraw()" title="Click and drag on the image to select a new ROI">📐 Draw ROI</button>
      <label style="display:flex;align-items:center;gap:5px;font-size:12px;cursor:pointer;" title="When checked, the ROI is applied immediately on mouse release">
        <input type="checkbox" id="roi-auto-apply">
        Auto-apply
      </label>
      <a id="save-btn" class="btn btn-ghost btn-sm" href="/snapshot" download>💾 Save Image</a>
      <div class="ml-auto flex-row" style="gap:4px;">
        <span style="font-size:11px;color:var(--muted);">LED:</span>
        <button class="btn btn-ghost btn-sm" id="led-btn" onclick="toggleLED()">—</button>
      </div>
    </div>
  </div>

</div>

<div class="toast" id="toast"></div>

<script>
// ── State ───────────────────────────────────────────────────────────────────

let _ledState  = null;
let _streaming = true;
let _acquiring = false;

// PV sections — keys are the actual PV suffixes under the IOC prefix.
let _cameraType = null;
let PV_SECTIONS  = [];

function buildPVSections(cameraType) {
  const sections = [
    { label: 'Acquisition', keys: ['cam1:AcquirePeriod'] },
    { label: 'Exposure', keys: ['cam1:AeEnable','cam1:AcquireTime','cam1:Gain'] },
  ];
  if (cameraType === 'imx708') {
    sections.push({ label: 'Autofocus', keys: ['cam1:AfMode','cam1:LensPosition'] });
    sections.push({ label: 'Sensor Mode', keys: ['cam1:SensorMode'] });
    sections.push({ label: 'Image Quality', keys: [
      'cam1:Brightness','cam1:Contrast','cam1:Sharpness','cam1:NoiseReductionMode',
    ]});
  }
  if (cameraType !== 'mock') {
    sections.push({ label: 'Orientation', keys: ['cam1:HFlip','cam1:VFlip'] });
  }
  sections.push({ label: 'Region of Interest', keys: [
    'cam1:MinX','cam1:MinY','cam1:SizeX','cam1:SizeY',
  ]});
  if (cameraType !== 'mock') {
    sections.push({ label: 'LED', keys: ['cam1:LedEnable'] });
  }
  sections.push({ label: 'Calibration (µm/pixel)', keys: [
    'cam1:CalibX','cam1:CalibY',
  ]});
  const readbacks = [
    'cam1:Model_RBV','cam1:Acquire_RBV','cam1:ArrayRate_RBV',
    'cam1:MinX_RBV','cam1:MinY_RBV','cam1:SizeX_RBV','cam1:SizeY_RBV',
    'cam1:MaxSizeX_RBV','cam1:MaxSizeY_RBV',
    'image1:ArraySize0_RBV','image1:ArraySize1_RBV',
    'cam1:BitsPerPixel_RBV',
  ];
  if (cameraType !== 'mock') readbacks.push('cam1:LedStatus_RBV','cam1:CpuTemp_RBV');
  sections.push({ label: 'Readbacks (read-only)', keys: readbacks });
  return sections;
}

// Step sizes for float inputs
const FLOAT_SUFFIXES = new Set([
  'cam1:AcquirePeriod','cam1:AcquireTime','cam1:Gain','cam1:Brightness',
  'cam1:Contrast','cam1:Sharpness','cam1:LensPosition',
  'cam1:CalibX','cam1:CalibY',
]);

// ── Utilities ────────────────────────────────────────────────────────────

function toast(msg, type='ok') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show ' + type;
  clearTimeout(el._t);
  el._t = setTimeout(() => el.className = 'toast', 2800);
}

function fmtUptime(s) {
  if (s == null) return '—';
  s = Math.round(s);
  return `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m`;
}

function fmtTimestamp(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleTimeString();
}

// ── Status ────────────────────────────────────────────────────────────────

function _updateAcquireBtn() {
  const btn = document.getElementById('acquire-btn');
  btn.textContent = _acquiring ? '⏸ Stop' : '▶ Acquire';
  btn.className = 'btn btn-sm ' + (_acquiring ? 'btn-warn' : 'btn-success');
}

async function refreshStatus() {
  try {
    const d = await fetch('/api/status').then(r => r.json());
    document.getElementById('s-hostname').textContent = d.hostname ?? '—';
    document.getElementById('s-ip').textContent       = d.ip       ?? '—';
    document.getElementById('s-uptime').textContent   = fmtUptime(d.uptime_s);
    document.getElementById('s-temp').textContent     = d.cpu_temp_c != null ? Number(d.cpu_temp_c).toFixed(1) + ' °C' : '—';
    document.getElementById('s-frame').textContent    = fmtTimestamp(d.last_frame);
    document.getElementById('s-rate').textContent     = d.frame_rate != null ? Number(d.frame_rate).toFixed(2) + ' Hz' : '—';
    _acquiring = !!d.acquiring;
    _updateAcquireBtn();
    const dot  = document.getElementById('ioc-dot');
    const text = document.getElementById('ioc-text');
    text.textContent = d.ioc_status ?? '—';
    dot.className = 'dot ' + (d.ioc_status === 'active' ? 'green' : d.ioc_status === 'activating' ? 'yellow' : 'red');
    if (d.camera_type && d.camera_type !== _cameraType) {
      _cameraType = d.camera_type;
      PV_SECTIONS = buildPVSections(_cameraType);
      document.getElementById('pv-sections').innerHTML = '';
      refreshPVs();
    }
  } catch(e) { console.warn('Status fetch failed', e); }
}

// ── PV controls ────────────────────────────────────────────────────────────

let pvData = {};

function buildPVTable(pvs) {
  pvData = pvs;

  if (pvs['cam1:LedEnable']?.ok) {
    _ledState = Number(pvs['cam1:LedEnable'].value);
    const btn = document.getElementById('led-btn');
    btn.textContent = _ledState ? '● ON' : '○ OFF';
    btn.className = 'btn btn-sm ' + (_ledState ? 'btn-success' : 'btn-ghost');
  }

  const container = document.getElementById('pv-sections');

  // In-place value update; never overwrite an input that has focus.
  if (container.hasChildNodes()) {
    for (const section of PV_SECTIONS) {
      for (const key of section.keys) {
        const pv = pvs[key];
        if (!pv) continue;
        if (pv.rw && pv.ok) {
          const inp = document.getElementById('inp-' + key);
          if (inp && inp !== document.activeElement) {
            inp.value = pv.value ?? '';
          }
        } else {
          const cell = document.getElementById('ro-' + key);
          if (cell) {
            cell.textContent = pv.ok ? (pv.value ?? '—') : '⚠ ' + (pv.error ?? 'error');
            cell.style.color = pv.ok ? '' : 'var(--danger)';
          }
        }
      }
    }
    return;
  }

  // First-time full build
  for (const section of PV_SECTIONS) {
    const label = document.createElement('div');
    label.className = 'section-label';
    label.textContent = section.label;
    container.appendChild(label);

    if (section.label === 'Region of Interest') {
      const row = document.createElement('div');
      row.className = 'action-row';
      row.innerHTML = `
        <button class="btn btn-primary btn-sm" onclick="applyROI()">✓ Apply ROI</button>
        <button class="btn btn-ghost btn-sm"   onclick="doAction('roi_reset')">↺ Reset ROI</button>
      `;
      container.appendChild(row);
    }

    const table = document.createElement('table');
    table.className = 'pv-table';
    container.appendChild(table);

    for (const key of section.keys) {
      const pv = pvs[key];
      if (!pv) continue;
      const tr = document.createElement('tr');

      const tdLabel = document.createElement('td');
      tdLabel.className = 'pv-label';
      tdLabel.textContent = pv.label;
      tr.appendChild(tdLabel);

      const tdValue = document.createElement('td');
      if (!pv.rw || !pv.ok) {
        tdValue.className = 'pv-value';
        tdValue.id = 'ro-' + key;
        tdValue.textContent = pv.ok ? (pv.value ?? '—') : '⚠ ' + (pv.error ?? 'error');
        if (!pv.ok) tdValue.style.color = 'var(--danger)';
      } else {
        const wrap = document.createElement('div');
        wrap.className = 'pv-input';
        const inp = document.createElement('input');
        inp.type  = 'number';
        inp.value = pv.value ?? '';
        inp.id    = 'inp-' + key;
        inp.step  = FLOAT_SUFFIXES.has(key) ? '0.1' : '1';
        const btn = document.createElement('button');
        btn.className = 'btn btn-primary btn-sm';
        btn.textContent = '✓';
        btn.onclick = () => writePV(key, inp.value);
        wrap.appendChild(inp);
        wrap.appendChild(btn);
        tdValue.appendChild(wrap);
      }
      tr.appendChild(tdValue);
      table.appendChild(tr);
    }
  }
}

async function refreshPVs() {
  try {
    const d = await fetch('/api/pvs').then(r => r.json());
    if (d.ok) {
      buildPVTable(d.pvs);
    }
  } catch(e) { toast('Failed to read PVs: ' + e, 'err'); }
}

async function writePV(suffix, value) {
  try {
    const r = await fetch('/api/pvs', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({pv: suffix, value}),
    });
    const d = await r.json();
    if (d.ok) {
      toast('✓ ' + suffix + ' = ' + value);
      if (suffix === 'cam1:LedEnable') {
        _ledState = Number(value);
        const btn = document.getElementById('led-btn');
        btn.textContent = _ledState ? '● ON' : '○ OFF';
        btn.className = 'btn btn-sm ' + (_ledState ? 'btn-success' : 'btn-ghost');
      }
    } else {
      toast('✗ ' + (d.error ?? 'write failed'), 'err');
    }
  } catch(e) { toast('✗ ' + e, 'err'); }
}

async function doAction(action) {
  try {
    const r = await fetch('/api/action', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({action}),
    });
    const d = await r.json();
    if (d.ok) {
      toast('✓ ' + action);
      setTimeout(refreshPVs, 800);
      setTimeout(refreshStatus, 800);
    } else {
      toast('✗ ' + (d.error ?? action + ' failed'), 'err');
    }
  } catch(e) { toast('✗ ' + e, 'err'); }
}

async function toggleAcquire() {
  await doAction(_acquiring ? 'acquire_stop' : 'acquire_start');
  _acquiring = !_acquiring;
  _updateAcquireBtn();
}

// applyROI — writes MinX/MinY/SizeX/SizeY in one request.  Standard-AD ROI
// records apply immediately on write; there is no staged apply step.
async function applyROI(x, y, w, h) {
  if (x === undefined) {
    x = parseInt(document.getElementById('inp-cam1:MinX')?.value  ?? 0);
    y = parseInt(document.getElementById('inp-cam1:MinY')?.value  ?? 0);
    w = parseInt(document.getElementById('inp-cam1:SizeX')?.value ?? 0);
    h = parseInt(document.getElementById('inp-cam1:SizeY')?.value ?? 0);
  }
  if (isNaN(x) || isNaN(y) || isNaN(w) || isNaN(h) || w <= 0 || h <= 0) {
    toast('✗ Invalid ROI values', 'err');
    return;
  }
  try {
    const r = await fetch('/api/roi_apply', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({x, y, w, h}),
    });
    const d = await r.json();
    if (d.ok) {
      toast(`✓ ROI applied: ${w}×${h} at (${x},${y})`);
      setTimeout(refreshPVs, 800);
    } else {
      toast('✗ ' + (d.error ?? 'ROI apply failed'), 'err');
    }
  } catch(e) { toast('✗ ' + e, 'err'); }
}

// ── Config editor ──────────────────────────────────────────────────────────

async function loadConfig() {
  try {
    const d = await fetch('/api/config').then(r => r.json());
    if (d.ok) {
      document.getElementById('config-editor').value = jsYamlDump(d.config);
      document.getElementById('config-msg').textContent = '';
    } else {
      setConfigMsg('✗ ' + d.error, true);
    }
  } catch(e) { setConfigMsg('✗ ' + e, true); }
}

async function saveConfig() {
  const raw = document.getElementById('config-editor').value;
  let parsed;
  try { parsed = jsYamlParse(raw); }
  catch(e) { setConfigMsg('✗ YAML parse error: ' + e, true); return; }
  try {
    const d = await fetch('/api/config', {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(parsed),
    }).then(r => r.json());
    if (d.ok) { setConfigMsg('✓ Saved — restart IOC to apply', false); toast('Config saved'); }
    else setConfigMsg('✗ ' + d.error, true);
  } catch(e) { setConfigMsg('✗ ' + e, true); }
}

function setConfigMsg(msg, err) {
  const el = document.getElementById('config-msg');
  el.textContent = msg;
  el.style.color = err ? 'var(--danger)' : 'var(--accent2)';
}

// ── IOC restart ────────────────────────────────────────────────────────────

async function restartIOC() {
  if (!confirm('Restart the vpcam IOC service?')) return;
  try {
    const d = await fetch('/api/restart', {method:'POST'}).then(r => r.json());
    if (d.ok) { toast('IOC restarting…'); setTimeout(refreshStatus, 3000); }
    else toast('✗ ' + (d.error ?? 'restart failed'), 'err');
  } catch(e) { toast('✗ ' + e, 'err'); }
}

// ── LED toggle ─────────────────────────────────────────────────────────────

async function toggleLED() {
  await writePV('cam1:LedEnable', _ledState ? 0 : 1);
}

// ── MJPEG stream ───────────────────────────────────────────────────────────

function startStream() {
  const img    = document.getElementById('live-img');
  const status = document.getElementById('stream-status');
  img.src = '/stream?' + Date.now();
  img.style.display = 'block';
  img.onload = () => {
    status.style.display = 'none';
    document.getElementById('stream-dot').className = 'dot green';
    document.getElementById('stream-label').textContent = 'live';
  };
  img.onerror = () => {
    status.textContent = 'Stream unavailable — IOC may be offline';
    status.style.display = '';
    document.getElementById('stream-dot').className = 'dot red';
    document.getElementById('stream-label').textContent = 'offline';
    if (_streaming) setTimeout(startStream, 5000);
  };
}

function toggleStream(on) {
  _streaming = on;
  if (on) {
    startStream();
  } else {
    document.getElementById('live-img').src = '';
    document.getElementById('stream-dot').className = 'dot yellow';
    document.getElementById('stream-label').textContent = 'paused';
  }
}

// ── Tabs ────────────────────────────────────────────────────────────────────

function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === 'panel-' + name));
  if (name === 'config' && !document.getElementById('config-editor').value) loadConfig();
}

// ── Minimal YAML serialiser/parser ──────────────────────────────────────────

function jsYamlDump(obj, indent=0) {
  const pad = '  '.repeat(indent);
  if (obj === null || obj === undefined) return 'null';
  if (typeof obj === 'boolean')  return obj ? 'true' : 'false';
  if (typeof obj === 'number')   return String(obj);
  if (typeof obj === 'string')   return /[\n:#\[\]{},]/.test(obj) ? JSON.stringify(obj) : obj;
  if (Array.isArray(obj))        return obj.map(v => pad + '- ' + jsYamlDump(v)).join('\n');
  if (typeof obj === 'object') {
    return Object.entries(obj).map(([k,v]) => {
      const isObj = v !== null && typeof v === 'object' && !Array.isArray(v);
      return pad + k + ':\n' + (isObj ? jsYamlDump(v, indent+1) : pad + '  ' + jsYamlDump(v));
    }).join('\n');
  }
  return String(obj);
}

function jsYamlParse(text) {
  const lines = text.replace(/\r/g,'').split('\n');
  const root  = {};
  const stack = [{indent:-1, obj:root}];
  for (let raw of lines) {
    const trimmed = raw.replace(/#.*$/,'').trimEnd();
    if (!trimmed.trim()) continue;
    const indent = trimmed.length - trimmed.trimStart().length;
    const line   = trimmed.trim();
    while (stack.length > 1 && indent <= stack[stack.length-1].indent) stack.pop();
    const parent = stack[stack.length-1].obj;
    const m = line.match(/^([^:]+):\s*(.*)$/);
    if (!m) continue;
    const key = m[1].trim();
    const val = m[2].trim();
    if (!val) {
      const child = {};
      parent[key] = child;
      stack.push({indent, obj:child});
    } else {
      if (val === 'true')  { parent[key] = true;  continue; }
      if (val === 'false') { parent[key] = false; continue; }
      if (val === 'null' || val === '~') { parent[key] = null; continue; }
      const n = Number(val);
      parent[key] = isNaN(n) ? val.replace(/^['"]|['"]$/g,'') : n;
    }
  }
  return root;
}

// ── ROI Canvas Drawing ──────────────────────────────────────────────────────
//
// Click "📐 Draw ROI", then click and drag on the live image to select a
// region.  On release the four ROI fields are populated with sensor-pixel
// coordinates; click "✓ Apply ROI" (or check Auto-apply) to send them.
//
// The canvas covers the ENTIRE image-wrap div; the image floats centered
// inside it.  For each event we look up the image's current position via
// getBoundingClientRect and convert mouse → image → sensor coordinates.

let _roiDrawMode = false;
let _roiDrag     = null;   // {sx,sy,ex,ey} in image-relative pixels

function _imgRect() {
  const img  = document.getElementById('live-img');
  const wrap = img.parentElement;
  const wr   = wrap.getBoundingClientRect();
  const ir   = img.getBoundingClientRect();
  return {
    left:   ir.left - wr.left,
    top:    ir.top  - wr.top,
    width:  ir.width,
    height: ir.height,
  };
}

function _evtToImg(e) {
  const canvas = document.getElementById('roi-canvas');
  const wr     = canvas.getBoundingClientRect();
  const src    = e.touches ? e.touches[0] : e;
  const dprX   = canvas.width  / wr.width;
  const dprY   = canvas.height / wr.height;
  const cx     = (src.clientX - wr.left) * dprX;
  const cy     = (src.clientY - wr.top)  * dprY;
  const r      = _imgRect();
  const iLeft  = r.left  * dprX;
  const iTop   = r.top   * dprY;
  const iW     = r.width * dprX;
  const iH     = r.height * dprY;
  return {
    x: Math.max(0, Math.min(iW, cx - iLeft)),
    y: Math.max(0, Math.min(iH, cy - iTop)),
    inBounds: cx >= iLeft && cx <= iLeft + iW && cy >= iTop && cy <= iTop + iH,
    iLeft, iTop, iW, iH,
  };
}

// Convert image-relative pixel rect to sensor pixel rect.  The MJPEG stream
// shows the active ROI region, so scale via SizeX/Y_RBV and offset by
// MinX/Y_RBV.
function _imgToSensor(ix, iy, iw, ih, iW, iH) {
  const roiW = Number(pvData['cam1:SizeX_RBV']?.value) || Number(pvData['image1:ArraySize0_RBV']?.value) || 1456;
  const roiH = Number(pvData['cam1:SizeY_RBV']?.value) || Number(pvData['image1:ArraySize1_RBV']?.value) || 1088;
  const roiX = Number(pvData['cam1:MinX_RBV']?.value)  || 0;
  const roiY = Number(pvData['cam1:MinY_RBV']?.value)  || 0;
  const scaleX = roiW / iW;
  const scaleY = roiH / iH;
  return {
    sensorX: Math.max(0, Math.round(roiX + ix * scaleX)),
    sensorY: Math.max(0, Math.round(roiY + iy * scaleY)),
    sensorW: Math.max(2, Math.round(iw * scaleX)),
    sensorH: Math.max(2, Math.round(ih * scaleY)),
  };
}

function toggleROIDraw() {
  _roiDrawMode = !_roiDrawMode;
  const btn    = document.getElementById('roi-draw-btn');
  const canvas = document.getElementById('roi-canvas');
  btn.classList.toggle('active', _roiDrawMode);
  if (_roiDrawMode) {
    // Refresh readbacks so _imgToSensor has current MinX/Y_RBV, SizeX/Y_RBV
    refreshPVs();
    const wrap   = canvas.parentElement;
    canvas.width  = wrap.clientWidth;
    canvas.height = wrap.clientHeight;
    canvas.classList.add('draw-active');
    toast('Draw ROI: click and drag on the image, then click Apply ROI');
  } else {
    _roiDrag = null;
    canvas.classList.remove('draw-active');
    canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
  }
}

function _redrawCanvas() {
  const canvas = document.getElementById('roi-canvas');
  const ctx    = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!_roiDrag) return;

  const wrap  = canvas.parentElement;
  const dprX  = canvas.width  / wrap.clientWidth;
  const dprY  = canvas.height / wrap.clientHeight;
  const r     = _imgRect();
  const iLeft = r.left  * dprX;
  const iTop  = r.top   * dprY;
  const iW    = r.width * dprX;
  const iH    = r.height * dprY;

  const sx = iLeft + _roiDrag.sx;
  const sy = iTop  + _roiDrag.sy;
  const ex = iLeft + _roiDrag.ex;
  const ey = iTop  + _roiDrag.ey;
  const rx = Math.min(sx, ex);
  const ry = Math.min(sy, ey);
  const rw = Math.abs(ex - sx);
  const rh = Math.abs(ey - sy);

  ctx.fillStyle = 'rgba(0,0,0,0.42)';
  ctx.fillRect(iLeft, iTop, iW, iH);
  ctx.clearRect(rx, ry, rw, rh);

  ctx.strokeStyle = '#4f8ef7';
  ctx.lineWidth   = 2;
  ctx.strokeRect(rx, ry, rw, rh);

  const hs = 6;
  ctx.fillStyle = '#4f8ef7';
  [[rx,ry],[rx+rw,ry],[rx,ry+rh],[rx+rw,ry+rh]].forEach(([hx,hy]) =>
    ctx.fillRect(hx - hs/2, hy - hs/2, hs, hs)
  );

  const {sensorX, sensorY, sensorW, sensorH} = _imgToSensor(
    Math.min(_roiDrag.sx, _roiDrag.ex),
    Math.min(_roiDrag.sy, _roiDrag.ey),
    Math.abs(_roiDrag.ex - _roiDrag.sx),
    Math.abs(_roiDrag.ey - _roiDrag.sy),
    iW, iH
  );
  const label  = `${sensorW} × ${sensorH}  (${sensorX}, ${sensorY})`;
  ctx.font     = 'bold 13px system-ui, sans-serif';
  ctx.shadowColor  = 'rgba(0,0,0,0.85)';
  ctx.shadowBlur   = 4;
  ctx.fillStyle    = '#ffffff';
  const textY = ry > 22 ? ry - 6 : ry + rh + 16;
  ctx.fillText(label, rx + 4, textY);
  ctx.shadowBlur = 0;
}

function _onCanvasDown(e) {
  if (!_roiDrawMode) return;
  const {x, y, inBounds} = _evtToImg(e);
  if (!inBounds) return;
  _roiDrag = {sx: x, sy: y, ex: x, ey: y};
  e.preventDefault();
}

function _onCanvasMove(e) {
  if (!_roiDrawMode || !_roiDrag) return;
  const {x, y} = _evtToImg(e);
  _roiDrag.ex = x;
  _roiDrag.ey = y;
  _redrawCanvas();
  e.preventDefault();
}

function _onCanvasUp(e) {
  if (!_roiDrawMode || !_roiDrag) return;
  const iw = Math.abs(_roiDrag.ex - _roiDrag.sx);
  const ih = Math.abs(_roiDrag.ey - _roiDrag.sy);

  const prev = {..._roiDrag};
  _roiDrag = null;
  const canvas = document.getElementById('roi-canvas');
  canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);

  if (iw < 8 || ih < 8) return; // too small — mis-click, ignore

  const wrap  = canvas.parentElement;
  const dprX  = canvas.width  / wrap.clientWidth;
  const dprY  = canvas.height / wrap.clientHeight;
  const r     = _imgRect();
  const iW    = r.width  * dprX;
  const iH    = r.height * dprY;

  const {sensorX, sensorY, sensorW, sensorH} = _imgToSensor(
    Math.min(prev.sx, prev.ex), Math.min(prev.sy, prev.ey), iw, ih, iW, iH
  );

  _setROIInputs(sensorX, sensorY, sensorW, sensorH);

  if (document.getElementById('roi-auto-apply')?.checked) {
    applyROI(sensorX, sensorY, sensorW, sensorH);
  } else {
    toast(`ROI set: ${sensorW}×${sensorH} at (${sensorX},${sensorY}) — click Apply ROI`);
  }
}

function _onCanvasLeave() {
  if (_roiDrawMode && _roiDrag) {
    _roiDrag = null;
    const canvas = document.getElementById('roi-canvas');
    canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
  }
}

function _setROIInputs(x, y, w, h) {
  const vals = {'cam1:MinX': x, 'cam1:MinY': y, 'cam1:SizeX': w, 'cam1:SizeY': h};
  for (const [key, val] of Object.entries(vals)) {
    const inp = document.getElementById('inp-' + key);
    if (inp) {
      inp.value = val;
      inp.style.borderColor = 'var(--accent)';
      setTimeout(() => { inp.style.borderColor = ''; }, 1800);
    }
  }
  const el = document.getElementById('inp-cam1:MinX');
  if (el) el.scrollIntoView({behavior:'smooth', block:'nearest'});
}

function _initROICanvas() {
  const canvas = document.getElementById('roi-canvas');
  canvas.addEventListener('mousedown',  _onCanvasDown);
  canvas.addEventListener('mousemove',  _onCanvasMove);
  canvas.addEventListener('mouseup',    _onCanvasUp);
  canvas.addEventListener('mouseleave', _onCanvasLeave);
  canvas.addEventListener('touchstart', _onCanvasDown, {passive: false});
  canvas.addEventListener('touchmove',  _onCanvasMove, {passive: false});
  canvas.addEventListener('touchend',   _onCanvasUp);
  window.addEventListener('resize', () => {
    if (!_roiDrawMode) return;
    const wrap   = canvas.parentElement;
    canvas.width  = wrap.clientWidth;
    canvas.height = wrap.clientHeight;
  });
}

// ── Boot ────────────────────────────────────────────────────────────────────

document.getElementById('pv-prefix-tag').textContent = 'PREFIX_TAG';
refreshStatus();
refreshPVs();
startStream();
setInterval(refreshStatus, 10000);
setInterval(refreshPVs,    5000);   // keep ROI readbacks current for canvas math
_initROICanvas();
</script>
</body>
</html>
"""


@app.route('/')
def index():
    return Response(HTML.replace('PREFIX_TAG', PREFIX + ':'),
                    mimetype='text/html')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    t = threading.Thread(target=_poll_camera, daemon=True, name='cam-poll')
    t.start()
    print(f"VPCam Web UI starting on http://0.0.0.0:{PORT}")
    print(f"PV prefix: {PREFIX}  |  Camera: {CAMERA_TYPE}  |  Config: {CONFIG_PATH}")
    app.run(host='0.0.0.0', port=PORT, threaded=True)
