import os
import time
os.environ['TZ'] = 'Asia/Jakarta'
if hasattr(time, 'tzset'):
    time.tzset()
import yaml
import shutil
import re
import subprocess
from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, send_from_directory, abort, jsonify, Response
)
from werkzeug.utils import secure_filename
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta

from app.recorder import CameraRecorder
from app.ptz import BardiPTZ
from app.gdrive import GDriveSync

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'cctv-bardi-production-secret-2024')

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.yaml')

def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, 'r') as f:
        cfg = yaml.safe_load(f) or {}

    # Migrate single camera to devices list if needed
    if 'devices' not in cfg:
        cam = cfg.get('camera', {})
        bardi_dev = {
            'id': 'bardi',
            'name': cam.get('name', 'Gang Tupan Timur (2K Bardi)'),
            'type': 'rtsp',
            'enabled': cam.get('enabled', True),
            'record': True,
            'ip': cam.get('ip', '192.168.1.3'),
            'port': cam.get('port', 554),
            'username': cam.get('username', 'admin'),
            'password': cam.get('password', 'admin'),
            'rtsp_path': cam.get('rtsp_path', '/V_ENC_000'),
            'onvif_port': 10000,
            'segment_seconds': cam.get('segment_seconds', 3600),
            'has_ptz': True
        }
        cfg['devices'] = [bardi_dev]

    return cfg

def save_config(cfg):
    with open(CONFIG_PATH, 'w') as f:
        yaml.safe_dump(cfg, f, default_flow_style=False)

def get_device_by_id(dev_id=None):
    cfg = load_config()
    devices = cfg.get('devices', [])
    if not devices:
        return cfg.get('camera', {})
    if not dev_id:
        return devices[0]
    for d in devices:
        if d.get('id') == dev_id:
            return d
    return devices[0]

cfg = load_config()
STORAGE_PATH = cfg.get('storage_path', '/recordings')
RETENTION_DAYS = cfg.get('retention_days', 30)
MAX_UPLOAD_MB = cfg.get('max_upload_size_mb', 1000)

app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_MB * 1024 * 1024
os.makedirs(STORAGE_PATH, exist_ok=True)

ALLOWED_EXTENSIONS = {'mp4', 'avi', 'mkv', 'mov', 'ts', 'flv', 'wmv', 'webm'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def human_size(nbytes):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"

FILE_PATTERN = re.compile(r'^([a-zA-Z0-9_\-]+)_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})\.mp4$')

def get_recordings(device_id=None):
    recordings = []
    if not os.path.isdir(STORAGE_PATH):
        return recordings
    for name in sorted(os.listdir(STORAGE_PATH), reverse=True):
        fp = os.path.join(STORAGE_PATH, name)
        if os.path.isfile(fp) and (name.endswith('.mp4') or name.endswith('.mkv')):
            m = FILE_PATTERN.match(name)
            file_dev = m.group(1) if m else "bardi"
            if device_id and device_id != 'all' and file_dev != device_id:
                continue
            stat = os.stat(fp)
            if stat.st_size > 10000:
                recordings.append({
                    'name': name,
                    'device_id': file_dev,
                    'size': human_size(stat.st_size),
                    'size_bytes': stat.st_size,
                    'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                })
    return recordings

def get_disk_usage():
    usage = shutil.disk_usage(STORAGE_PATH)
    return {
        'total': human_size(usage.total),
        'used': human_size(usage.used),
        'free': human_size(usage.free),
        'percent': round((usage.used / usage.total) * 100, 1),
    }

# ---------------------------------------------------------------------------
# Timeline Indexer Engine (24-Hour NVR Timeline)
# ---------------------------------------------------------------------------
def get_playback_dates(device_id=None):
    dates = set()
    if not os.path.isdir(STORAGE_PATH):
        return []
    for fname in os.listdir(STORAGE_PATH):
        m = FILE_PATTERN.match(fname)
        if m:
            file_dev = m.group(1)
            if device_id and device_id != 'all' and file_dev != device_id:
                continue
            dates.add(m.group(2))
    return sorted(list(dates), reverse=True)

def get_playback_timeline(target_date, device_id=None):
    segments = []
    if not os.path.isdir(STORAGE_PATH):
        return {"date": target_date, "device_id": device_id, "segments": []}

    files_for_date = []
    for fname in os.listdir(STORAGE_PATH):
        m = FILE_PATTERN.match(fname)
        if m and m.group(2) == target_date:
            file_dev = m.group(1)
            if device_id and device_id != 'all' and file_dev != device_id:
                continue
            hh, mm, ss = int(m.group(3)), int(m.group(4)), int(m.group(5))
            start_sec = hh * 3600 + mm * 60 + ss
            files_for_date.append((start_sec, fname, f"{hh:02d}:{mm:02d}:{ss:02d}"))

    files_for_date.sort(key=lambda x: x[0])

    for i, (sec, fname, time_str) in enumerate(files_for_date):
        fp = os.path.join(STORAGE_PATH, fname)
        size = os.path.getsize(fp) if os.path.exists(fp) else 0

        # Estimate duration until next segment or default 900s/3600s
        if i < len(files_for_date) - 1:
            duration = min(files_for_date[i + 1][0] - sec, 3600)
            if duration <= 0:
                duration = 900
        else:
            duration = 900

        segments.append({
            "filename": fname,
            "device_id": device_id,
            "start_time": time_str,
            "start_sec": sec,
            "end_sec": sec + duration,
            "duration": duration,
            "size": human_size(size),
            "size_bytes": size,
        })

    return {
        "date": target_date,
        "device_id": device_id,
        "total_segments": len(segments),
        "segments": segments
    }

# ---------------------------------------------------------------------------
# Background Cleanup Scheduler
# ---------------------------------------------------------------------------
def cleanup_old_recordings():
    curr_cfg = load_config()
    ret_days = curr_cfg.get('retention_days', 30)
    cutoff = datetime.now() - timedelta(days=ret_days)
    if not os.path.isdir(STORAGE_PATH):
        return
    deleted = 0
    for root, _, files in os.walk(STORAGE_PATH):
        for name in files:
            fp = os.path.join(root, name)
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(fp))
                if mtime < cutoff:
                    os.remove(fp)
                    deleted += 1
                    app.logger.info(f"Deleted old recording: {fp}")
            except Exception as e:
                app.logger.error(f"Error deleting {fp}: {e}")
    app.logger.info(f"Cleanup done: {deleted} files removed (Retention: {ret_days} days).")

scheduler = BackgroundScheduler()
scheduler.add_job(cleanup_old_recordings, 'cron', hour=2, minute=0, id='daily_cleanup')

def recorder_watchdog():
    try:
        recorder.sync_workers()
    except Exception as e:
        app.logger.error(f"Watchdog error: {e}")

scheduler.add_job(recorder_watchdog, 'interval', seconds=20, id='recorder_watchdog')
scheduler.start()

# ---------------------------------------------------------------------------
# Camera Recorder & PTZ & GDrive Instances
# ---------------------------------------------------------------------------
recorder = CameraRecorder(load_config)
gdrive_sync = GDriveSync(storage_path=STORAGE_PATH)

def get_ptz(device_id=None):
    dev = get_device_by_id(device_id)
    if not dev.get('has_ptz', False):
        cfg = load_config()
        ptz_dev = next((d for d in cfg.get('devices', []) if d.get('has_ptz', False)), None)
        if ptz_dev:
            dev = ptz_dev

    return BardiPTZ(
        ip=dev.get('ip', '192.168.1.3'),
        port=dev.get('onvif_port', 10000),
        user=dev.get('username', 'admin'),
        password=dev.get('password', 'admin')
    )

# Start multi-device recorder workers
recorder.start()

# ---------------------------------------------------------------------------
# Web Routes
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    curr_cfg = load_config()
    devices = curr_cfg.get('devices', [])
    active_id = request.args.get('device', devices[0]['id'] if devices else 'bardi')
    active_dev = next((d for d in devices if d.get('id') == active_id), devices[0] if devices else {})

    recordings = get_recordings(device_id=active_id)
    disk = get_disk_usage()
    total_size = sum(r['size_bytes'] for r in recordings)
    cam_status = recorder.get_status(active_id)
    playback_dates = get_playback_dates(device_id=active_id)
    gdrive_status = gdrive_sync.get_status()

    return render_template(
        'index.html',
        devices=devices,
        active_dev=active_dev,
        active_device_id=active_id,
        recordings=recordings,
        disk=disk,
        total_files=len(recordings),
        total_size=human_size(total_size),
        retention_days=curr_cfg.get('retention_days', 30),
        cam_status=cam_status,
        cam_cfg=active_dev,
        playback_dates=playback_dates,
        gdrive_status=gdrive_status,
    )

# ---------------------------------------------------------------------------
# Timeline & Playback API
# ---------------------------------------------------------------------------
@app.route('/api/playback/dates')
def api_playback_dates():
    dev_id = request.args.get('device')
    return jsonify(get_playback_dates(device_id=dev_id))

@app.route('/api/playback/timeline')
def api_playback_timeline():
    dev_id = request.args.get('device')
    target_date = request.args.get('date')
    if not target_date:
        today = datetime.now().strftime('%Y-%m-%d')
        available = get_playback_dates(device_id=dev_id)
        target_date = available[0] if available else today
    return jsonify(get_playback_timeline(target_date, device_id=dev_id))

@app.route('/api/gdrive/status')
def api_gdrive_status():
    return jsonify(gdrive_sync.get_status())

@app.route('/api/gdrive/sync', methods=['POST'])
def api_gdrive_sync():
    ok, msg = gdrive_sync.sync_once()
    return jsonify({"success": ok, "message": msg})

# ---------------------------------------------------------------------------
# Device Management API
# ---------------------------------------------------------------------------
@app.route('/api/devices')
def api_devices_list():
    curr_cfg = load_config()
    return jsonify(curr_cfg.get('devices', []))

@app.route('/api/devices/save', methods=['POST'])
def api_devices_save():
    curr_cfg = load_config()
    devices = curr_cfg.get('devices', [])
    data = request.get_json() or {}
    dev_id = data.get('id', '').strip()
    if not dev_id:
        dev_id = 'cam_' + str(int(time.time()))

    dev = {
        'id': dev_id,
        'name': data.get('name', 'Kamera').strip() or 'Kamera',
        'type': data.get('type', 'rtsp').strip().lower(),
        'enabled': bool(data.get('enabled', True)),
        'record': bool(data.get('record', False)),
        'segment_seconds': int(data.get('segment_seconds', 3600) or 3600),
        'has_ptz': bool(data.get('has_ptz', False)),
    }

    if dev['type'] == 'hls':
        dev['url'] = data.get('url', '').strip()
    else:
        dev['ip'] = data.get('ip', '').strip()
        dev['port'] = int(data.get('port', 554) or 554)
        dev['username'] = data.get('username', 'admin').strip()
        new_pw = data.get('password', '').strip()
        if new_pw:
            dev['password'] = new_pw
        else:
            existing = next((d for d in devices if d.get('id') == dev_id), None)
            dev['password'] = existing.get('password', 'admin') if existing else 'admin'
        dev['rtsp_path'] = data.get('rtsp_path', '/V_ENC_000').strip() or '/V_ENC_000'
        dev['onvif_port'] = int(data.get('onvif_port', 10000) or 10000)

    idx = next((i for i, d in enumerate(devices) if d.get('id') == dev_id), -1)
    if idx >= 0:
        devices[idx] = dev
    else:
        devices.append(dev)

    curr_cfg['devices'] = devices
    if devices:
        curr_cfg['camera'] = devices[0]
    save_config(curr_cfg)
    recorder.sync_workers()

    return jsonify({"success": True, "device": dev, "message": f'Perangkat "{dev["name"]}" berhasil disimpan.'})

@app.route('/api/devices/delete', methods=['POST'])
def api_devices_delete():
    curr_cfg = load_config()
    devices = curr_cfg.get('devices', [])
    data = request.get_json() or {}
    dev_id = data.get('id')
    if not dev_id:
        return jsonify({"success": False, "message": "ID perangkat tidak valid"}), 400
    if len(devices) <= 1:
        return jsonify({"success": False, "message": "Minimal harus ada 1 kamera terdaftar"}), 400

    devices = [d for d in devices if d.get('id') != dev_id]
    curr_cfg['devices'] = devices
    if devices:
        curr_cfg['camera'] = devices[0]
    save_config(curr_cfg)
    recorder.sync_workers()

    return jsonify({"success": True, "message": "Kamera berhasil dihapus."})

# ---------------------------------------------------------------------------
# HLS Smart CORS Proxy
# ---------------------------------------------------------------------------
@app.route('/proxy/hls/<device_id>/playlist.m3u8')
def proxy_hls_playlist(device_id):
    dev = get_device_by_id(device_id)
    if not dev or dev.get('type') != 'hls':
        abort(404)
    target_url = dev.get('url')
    if not target_url:
        abort(404)

    req = urllib.request.Request(target_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            raw = res.read().decode('utf-8', errors='replace')

        lines = []
        for line in raw.splitlines():
            sline = line.strip()
            if sline and not sline.startswith('#'):
                lines.append(f'/proxy/hls/{device_id}/{sline}')
            else:
                lines.append(line)

        body = '\n'.join(lines)
        response = Response(body, mimetype='application/vnd.apple.mpegurl')
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return response
    except Exception as e:
        app.logger.error(f"HLS proxy error: {e}")
        abort(502)

@app.route('/proxy/hls/<device_id>/<path:segment>')
def proxy_hls_segment(device_id, segment):
    dev = get_device_by_id(device_id)
    if not dev or dev.get('type') != 'hls':
        abort(404)
    base_url = dev.get('url').rsplit('/', 1)[0] + '/'
    target_url = base_url + segment

    req = urllib.request.Request(target_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    try:
        def stream_chunk():
            with urllib.request.urlopen(req, timeout=6) as res:
                while True:
                    data = res.read(65536)
                    if not data:
                        break
                    yield data
        response = Response(stream_chunk(), mimetype='video/mp2t')
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response
    except Exception as e:
        app.logger.error(f"HLS segment error: {e}")
        abort(502)

# ---------------------------------------------------------------------------
# Live View Streaming (MJPEG & Snapshots)
# ---------------------------------------------------------------------------
@app.route('/live/feed.mjpeg')
def live_mjpeg():
    """Real-time live video preview from active camera stream (RTSP or HLS)."""
    dev_id = request.args.get('device')
    dev = get_device_by_id(dev_id)
    dev_type = dev.get('type', 'rtsp').lower()

    if dev_type == 'hls':
        stream_url = dev.get('url', '')
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-i", stream_url,
            "-f", "mjpeg",
            "-q:v", "4",
            "-r", "15",
            "-an",
            "-"
        ]
    else:
        ip = dev.get('ip', '192.168.1.3')
        port = dev.get('port', 554)
        user = dev.get('username', 'admin')
        pwd = dev.get('password', 'admin')
        stream_path = dev.get('rtsp_path', '/V_ENC_000') or '/V_ENC_000'
        stream_url = f"rtsp://{user}:{pwd}@{ip}:{port}{stream_path}"
        cmd = [
            "ffmpeg",
            "-rtsp_transport", "tcp",
            "-timeout", "5000000",
            "-hide_banner",
            "-loglevel", "error",
            "-i", stream_url,
            "-f", "mjpeg",
            "-q:v", "3",
            "-r", "15",
            "-an",
            "-"
        ]

    def generate_frames():
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10**6)
        buffer = b""
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                buffer += chunk
                a = buffer.find(b'\xff\xd8')
                b = buffer.find(b'\xff\xd9')
                if a != -1 and b != -1:
                    jpg = buffer[a:b+2]
                    buffer = buffer[b+2:]
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + jpg + b'\r\n')
        except GeneratorExit:
            pass
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except Exception:
                proc.kill()

    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/live/audio')
@app.route('/live/audio.mp3')
def live_audio():
    """Real-time live audio streaming (transcoded to MP3 for universal browser support)."""
    dev_id = request.args.get('device')
    dev = get_device_by_id(dev_id)
    dev_type = dev.get('type', 'rtsp').lower()

    if dev_type == 'hls':
        stream_url = dev.get('url', '')
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-i", stream_url,
            "-vn",
            "-c:a", "libmp3lame",
            "-ar", "16000",
            "-b:a", "32k",
            "-f", "mp3",
            "-"
        ]
    else:
        ip = dev.get('ip', '192.168.1.3')
        port = dev.get('port', 554)
        user = dev.get('username', 'admin')
        pwd = dev.get('password', 'admin')
        stream_path = dev.get('rtsp_path', '/V_ENC_000') or '/V_ENC_000'
        stream_url = f"rtsp://{user}:{pwd}@{ip}:{port}{stream_path}"
        cmd = [
            "ffmpeg",
            "-rtsp_transport", "tcp",
            "-timeout", "5000000",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-hide_banner",
            "-loglevel", "error",
            "-i", stream_url,
            "-vn",
            "-c:a", "libmp3lame",
            "-ar", "16000",
            "-b:a", "32k",
            "-f", "mp3",
            "-"
        ]

    def generate_audio():
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=32768)
        try:
            while True:
                chunk = proc.stdout.read(2048)
                if not chunk:
                    break
                yield chunk
        except GeneratorExit:
            pass
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except Exception:
                proc.kill()

    resp = Response(generate_audio(), mimetype='audio/mpeg')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    return resp

@app.route('/live/snapshot.jpg')
def live_snapshot():
    """Capture a single instantaneous frame."""
    dev_id = request.args.get('device')
    dev = get_device_by_id(dev_id)
    dev_type = dev.get('type', 'rtsp').lower()

    if dev_type == 'hls':
        stream_url = dev.get('url', '')
        cmd = [
            "ffmpeg",
            "-y",
            "-i", stream_url,
            "-vframes", "1",
            "-q:v", "2",
            "-f", "image2",
            "-"
        ]
    else:
        ip = dev.get('ip', '192.168.1.3')
        port = dev.get('port', 554)
        user = dev.get('username', 'admin')
        pwd = dev.get('password', 'admin')
        stream_path = dev.get('rtsp_path', '/V_ENC_000') or '/V_ENC_000'
        stream_url = f"rtsp://{user}:{pwd}@{ip}:{port}{stream_path}"
        cmd = [
            "ffmpeg",
            "-y",
            "-rtsp_transport", "tcp",
            "-i", stream_url,
            "-vframes", "1",
            "-q:v", "2",
            "-f", "image2",
            "-"
        ]

    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        if res.returncode == 0:
            return Response(res.stdout, mimetype='image/jpeg')
        else:
            abort(500)
    except Exception:
        abort(504)

# ---------------------------------------------------------------------------
# ONVIF PTZ Endpoints
# ---------------------------------------------------------------------------
@app.route('/api/ptz/move', methods=['POST'])
def api_ptz_move():
    dev_id = request.args.get('device')
    direction = request.args.get('direction', 'stop').lower()
    speed = float(request.args.get('speed', 0.4))
    ptz = get_ptz(dev_id)
    ok, msg = ptz.move(direction=direction, speed=speed)
    return jsonify({"success": ok, "message": msg, "direction": direction})

@app.route('/api/ptz/stop', methods=['POST'])
def api_ptz_stop():
    dev_id = request.args.get('device')
    ptz = get_ptz(dev_id)
    ok, msg = ptz.stop()
    return jsonify({"success": ok, "message": msg})

# ---------------------------------------------------------------------------
# ONVIF Camera Control (Night Mode, Imaging, Siren/Relay, Presets, Reboot)
# ---------------------------------------------------------------------------
@app.route('/api/camera/imaging', methods=['GET', 'POST'])
def api_camera_imaging():
    data = request.get_json() if request.is_json else {}
    dev_id = request.args.get('device') or data.get('device')
    ptz = get_ptz(dev_id)
    if request.method == 'GET':
        ok, res_data = ptz.get_imaging()
        return jsonify({"success": ok, "data": res_data})

    ok, res = ptz.set_imaging(
        ircut=data.get('ircut'),
        brightness=data.get('brightness'),
        contrast=data.get('contrast'),
        saturation=data.get('saturation'),
        sharpness=data.get('sharpness')
    )
    return jsonify({"success": ok, "data": res if ok else None, "message": "Citra kamera diperbarui" if ok else str(res)})

@app.route('/api/camera/night-mode', methods=['POST'])
def api_camera_night_mode():
    data = request.get_json() or {}
    dev_id = request.args.get('device') or data.get('device')
    ptz = get_ptz(dev_id)
    mode = data.get('mode', 'AUTO').upper()
    ok, res = ptz.set_night_mode(mode)
    return jsonify({"success": ok, "mode": mode, "data": res if ok else None, "error": None if ok else str(res)})

@app.route('/api/camera/siren', methods=['POST'])
def api_camera_siren():
    data = request.get_json() or {}
    dev_id = request.args.get('device') or data.get('device')
    ptz = get_ptz(dev_id)
    state = data.get('state', 'active').lower()
    duration = int(data.get('duration', 0))

    ok, msg = ptz.set_relay(logical_state=state)
    if ok and state == 'active' and duration > 0:
        def auto_off():
            time.sleep(duration)
            try:
                ptz.set_relay(logical_state='inactive')
            except Exception:
                pass
        threading.Thread(target=auto_off, daemon=True).start()

    return jsonify({"success": ok, "message": msg, "state": state})

@app.route('/api/camera/presets', methods=['GET', 'POST'])
def api_camera_presets():
    data = request.get_json() if request.is_json else {}
    dev_id = request.args.get('device') or data.get('device')
    ptz = get_ptz(dev_id)
    if request.method == 'GET':
        ok, presets = ptz.get_presets()
        return jsonify({"success": ok, "presets": presets})

    action = data.get('action', '').lower()

    if action == 'goto':
        token = data.get('token')
        if not token:
            return jsonify({"success": False, "message": "Token preset tidak valid"}), 400
        ok, msg = ptz.goto_preset(token)
        return jsonify({"success": ok, "message": msg})

    elif action == 'set':
        name = data.get('name', 'Sudut Pantau')
        ok, res = ptz.set_preset(name)
        return jsonify({"success": ok, "preset": res if ok else None, "message": "Preset berhasil disimpan" if ok else str(res)})

    elif action == 'remove':
        token = data.get('token')
        if not token:
            return jsonify({"success": False, "message": "Token preset tidak valid"}), 400
        ok, msg = ptz.remove_preset(token)
        return jsonify({"success": ok, "message": msg})

    return jsonify({"success": False, "message": "Aksi tidak dikenali"}), 400

@app.route('/api/camera/reboot', methods=['POST'])
def api_camera_reboot():
    dev_id = request.args.get('device')
    ptz = get_ptz(dev_id)
    ok, msg = ptz.reboot()
    return jsonify({"success": ok, "message": msg})

# ---------------------------------------------------------------------------
# Settings & Control
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Settings & Control
# ---------------------------------------------------------------------------
@app.route('/settings', methods=['POST'])
def save_settings():
    curr_cfg = load_config()
    devices = curr_cfg.get('devices', [])
    dev_id = request.form.get('dev_id', '').strip()
    if not dev_id:
        dev_id = 'bardi'

    dev_type = request.form.get('type', 'rtsp').strip().lower()
    dev_name = request.form.get('name', '').strip() or ('Bardi PTZ' if dev_type == 'rtsp' else 'Public CCTV')
    should_record = request.form.get('record') == 'on' or request.form.get('record') == 'true' or request.form.get('record') == '1'

    # Build device dict
    dev = {
        'id': dev_id,
        'name': dev_name,
        'type': dev_type,
        'enabled': True,
        'record': should_record,
        'segment_seconds': int(request.form.get('segment_seconds', 3600) or 3600),
        'has_ptz': dev_type == 'rtsp',
    }

    if dev_type == 'hls':
        dev['url'] = request.form.get('url', '').strip()
    else:
        dev['ip'] = request.form.get('ip', '192.168.1.3').strip()
        dev['port'] = int(request.form.get('port', 554) or 554)
        dev['username'] = request.form.get('username', 'admin').strip()
        new_pw = request.form.get('password', '').strip()
        if new_pw:
            dev['password'] = new_pw
        else:
            existing = next((d for d in devices if d.get('id') == dev_id), None)
            dev['password'] = existing.get('password', 'admin') if existing else 'admin'
        dev['rtsp_path'] = request.form.get('rtsp_path', '/V_ENC_000').strip() or '/V_ENC_000'
        dev['onvif_port'] = int(request.form.get('onvif_port', 10000) or 10000)

    # Save to devices
    idx = next((i for i, d in enumerate(devices) if d.get('id') == dev_id), -1)
    if idx >= 0:
        devices[idx] = dev
    else:
        devices.append(dev)

    curr_cfg['devices'] = devices
    if devices:
        curr_cfg['camera'] = devices[0]
    curr_cfg['retention_days'] = int(request.form.get('retention_days', 30) or 30)
    save_config(curr_cfg)

    # Sync autonomous recording workers
    recorder.sync_workers()
    rec_text = "Merekam 24/7 Aktif" if should_record else "Live View Saja (Tidak Rekam)"
    flash(f'Pengaturan kamera "{dev["name"]}" disimpan. Status: {rec_text}.', 'success')

    return redirect(url_for('index', device=dev_id))

@app.route('/recorder/start', methods=['POST'])
def recorder_start():
    recorder.start()
    flash('Perekaman otomatis multi-kamera disinkronkan & dijalankan.', 'success')
    return redirect(url_for('index'))

@app.route('/recorder/stop', methods=['POST'])
def recorder_stop():
    recorder.stop()
    flash('Semua proses perekaman dihentikan.', 'info')
    return redirect(url_for('index'))

@app.route('/recorder/test', methods=['POST'])
def recorder_test():
    curr_cfg = load_config()
    data = request.get_json() if request.is_json else {}
    dev_id = data.get('id') or request.args.get('device')
    if dev_id:
        dev = next((d for d in curr_cfg.get('devices', []) if d.get('id') == dev_id), None)
        cam = dict(dev) if dev else data
        cam.update(data)
    else:
        cam = dict(curr_cfg.get('devices', [{}])[0])
        cam.update(data)
    ok, msg = recorder.test_connection(cam)
    return jsonify({"success": ok, "message": msg})

@app.route('/recorder/status')
def recorder_status_api():
    dev_id = request.args.get('device')
    return jsonify(recorder.get_status(dev_id))

# ---------------------------------------------------------------------------
# Universal In-Browser Video Playback (H.264 On-The-Fly Web Streamer)
# ---------------------------------------------------------------------------
@app.route('/video/stream/<path:filename>')
def video_stream_h264(filename):
    """
    On-the-fly H.264 streaming.
    Ensures 100% universal playback across all browsers (Chrome, Firefox, Safari, Edge, Mobile)
    without codec or unfinalized moov atom errors!
    """
    safe = secure_filename(filename)
    path = os.path.join(STORAGE_PATH, safe)
    if not os.path.isfile(path):
        abort(404)

    start_sec = request.args.get('t', '0')
    try:
        float(start_sec)
    except ValueError:
        start_sec = '0'

    cmd = [
        "ffmpeg",
        "-ss", str(start_sec),
        "-i", path,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-crf", "18",                    # Visually lossless high-clarity 2K streaming
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "64k",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4",
        "-"
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def generate():
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        except GeneratorExit:
            pass
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except Exception:
                proc.kill()

    resp = Response(generate(), mimetype='video/mp4')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

@app.route('/video/raw/<path:filename>')
@app.route('/video/<path:filename>')
def stream_video_raw(filename):
    """Raw MP4 range-streaming endpoint."""
    safe = secure_filename(filename)
    path = os.path.join(STORAGE_PATH, safe)
    if not os.path.isfile(path):
        abort(404)

    range_header = request.headers.get('Range', None)
    if not range_header:
        return send_from_directory(STORAGE_PATH, safe)

    size = os.path.getsize(path)
    byte1, byte2 = 0, None
    m = re.search(r'(\d+)-(\d*)', range_header)
    if m:
        g = m.groups()
        byte1 = int(g[0])
        if g[1]:
            byte2 = int(g[1])

    if byte2 is None:
        byte2 = size - 1

    length = byte2 - byte1 + 1

    def generate():
        with open(path, 'rb') as f:
            f.seek(byte1)
            remaining = length
            chunk_size = 1024 * 512
            while remaining > 0:
                read_bytes = min(remaining, chunk_size)
                data = f.read(read_bytes)
                if not data:
                    break
                remaining -= len(data)
                yield data

    resp = Response(
        generate(),
        status=206,
        mimetype='video/mp4',
        direct_passthrough=True
    )
    resp.headers.add('Content-Range', f'bytes {byte1}-{byte2}/{size}')
    resp.headers.add('Accept-Ranges', 'bytes')
    resp.headers.add('Content-Length', str(length))
    return resp

@app.route('/download/<path:filename>')
def download(filename):
    safe = secure_filename(filename)
    path = os.path.join(STORAGE_PATH, safe)
    if not os.path.isfile(path):
        abort(404)
    return send_from_directory(STORAGE_PATH, safe, as_attachment=True)

@app.route('/delete/<path:filename>', methods=['POST'])
def delete(filename):
    safe = secure_filename(filename)
    path = os.path.join(STORAGE_PATH, safe)
    if os.path.isfile(path):
        os.remove(path)
        flash(f'File "{safe}" berhasil dihapus.', 'success')
    else:
        flash('File tidak ditemukan.', 'error')
    return redirect(url_for('index'))

@app.route('/health')
def health():
    return jsonify({
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "recordings_count": len(get_recordings()),
        "recorder": recorder.get_status()
    })

@app.route('/api/recordings')
def api_recordings():
    return jsonify(get_recordings())

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
