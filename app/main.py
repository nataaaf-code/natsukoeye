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
        return yaml.safe_load(f) or {}

def save_config(cfg):
    with open(CONFIG_PATH, 'w') as f:
        yaml.safe_dump(cfg, f, default_flow_style=False)

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

def get_recordings():
    recordings = []
    if not os.path.isdir(STORAGE_PATH):
        return recordings
    for name in sorted(os.listdir(STORAGE_PATH), reverse=True):
        fp = os.path.join(STORAGE_PATH, name)
        if os.path.isfile(fp) and (name.endswith('.mp4') or name.endswith('.mkv')):
            stat = os.stat(fp)
            if stat.st_size > 10000:
                recordings.append({
                    'name': name,
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
FILE_PATTERN = re.compile(r'bardi_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})\.mp4')

def get_playback_dates():
    dates = set()
    if not os.path.isdir(STORAGE_PATH):
        return []
    for fname in os.listdir(STORAGE_PATH):
        m = FILE_PATTERN.match(fname)
        if m:
            dates.add(m.group(1))
    return sorted(list(dates), reverse=True)

def get_playback_timeline(target_date):
    segments = []
    if not os.path.isdir(STORAGE_PATH):
        return {"date": target_date, "segments": []}

    files_for_date = []
    for fname in os.listdir(STORAGE_PATH):
        m = FILE_PATTERN.match(fname)
        if m and m.group(1) == target_date:
            hh, mm, ss = int(m.group(2)), int(m.group(3)), int(m.group(4))
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
            "start_time": time_str,
            "start_sec": sec,
            "end_sec": sec + duration,
            "duration": duration,
            "size": human_size(size),
            "size_bytes": size,
        })

    return {
        "date": target_date,
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
        curr_cfg = load_config()
        cam_cfg = curr_cfg.get('camera', {})
        if cam_cfg.get('enabled', False):
            if not recorder.is_alive():
                app.logger.warning("Watchdog: Recorder thread is not alive! Reviving recorder...")
                recorder.start()
    except Exception as e:
        app.logger.error(f"Watchdog error: {e}")

scheduler.add_job(recorder_watchdog, 'interval', seconds=20, id='recorder_watchdog')
scheduler.start()

# ---------------------------------------------------------------------------
# Camera Recorder & PTZ & GDrive Instances
# ---------------------------------------------------------------------------
recorder = CameraRecorder(load_config)
gdrive_sync = GDriveSync(storage_path=STORAGE_PATH)

def get_ptz():
    curr_cfg = load_config()
    cam = curr_cfg.get('camera', {})
    return BardiPTZ(
        ip=cam.get('ip', '192.168.1.3'),
        port=10000,
        user=cam.get('username', 'admin'),
        password=cam.get('password', 'admin')
    )

# Start recorder automatically if camera is configured & enabled
initial_cfg = load_config()
if initial_cfg.get('camera', {}).get('enabled', False):
    recorder.start()

# ---------------------------------------------------------------------------
# Web Routes
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    curr_cfg = load_config()
    recordings = get_recordings()
    disk = get_disk_usage()
    total_size = sum(r['size_bytes'] for r in recordings)
    cam_status = recorder.get_status()
    cam_cfg = curr_cfg.get('camera', {})
    playback_dates = get_playback_dates()
    gdrive_status = gdrive_sync.get_status()

    return render_template(
        'index.html',
        recordings=recordings,
        disk=disk,
        total_files=len(recordings),
        total_size=human_size(total_size),
        retention_days=curr_cfg.get('retention_days', 30),
        cam_status=cam_status,
        cam_cfg=cam_cfg,
        playback_dates=playback_dates,
        gdrive_status=gdrive_status,
    )

# ---------------------------------------------------------------------------
# Timeline & Playback API
# ---------------------------------------------------------------------------
@app.route('/api/playback/dates')
def api_playback_dates():
    return jsonify(get_playback_dates())

@app.route('/api/playback/timeline')
def api_playback_timeline():
    target_date = request.args.get('date')
    if not target_date:
        today = datetime.now().strftime('%Y-%m-%d')
        available = get_playback_dates()
        target_date = available[0] if available else today
    return jsonify(get_playback_timeline(target_date))

@app.route('/api/gdrive/status')
def api_gdrive_status():
    return jsonify(gdrive_sync.get_status())

@app.route('/api/gdrive/sync', methods=['POST'])
def api_gdrive_sync():
    ok, msg = gdrive_sync.sync_once()
    return jsonify({"success": ok, "message": msg})

# ---------------------------------------------------------------------------
# Live View Streaming (MJPEG & Snapshots)
# ---------------------------------------------------------------------------
@app.route('/live/feed.mjpeg')
def live_mjpeg():
    """Real-time 2K live video preview from camera stream."""
    curr_cfg = load_config()
    cam_cfg = curr_cfg.get('camera', {})
    ip = cam_cfg.get('ip', '192.168.1.3')
    port = cam_cfg.get('port', 554)
    user = cam_cfg.get('username', 'admin')
    pwd = cam_cfg.get('password', 'admin')
    stream_path = cam_cfg.get('rtsp_path', '/V_ENC_000') or '/V_ENC_000'
    live_url = f"rtsp://{user}:{pwd}@{ip}:{port}{stream_path}"

    def generate_frames():
        cmd = [
            "ffmpeg",
            "-rtsp_transport", "tcp",
            "-hide_banner",
            "-loglevel", "error",
            "-i", live_url,
            "-f", "mjpeg",
            "-q:v", "3",
            "-r", "15",
            "-an",
            "-"
        ]
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

@app.route('/live/snapshot.jpg')
def live_snapshot():
    """Capture a single 2K instantaneous frame."""
    curr_cfg = load_config()
    cam_cfg = curr_cfg.get('camera', {})
    ip = cam_cfg.get('ip', '192.168.1.3')
    port = cam_cfg.get('port', 554)
    user = cam_cfg.get('username', 'admin')
    pwd = cam_cfg.get('password', 'admin')
    stream_path = cam_cfg.get('rtsp_path', '/V_ENC_000') or '/V_ENC_000'
    live_url = f"rtsp://{user}:{pwd}@{ip}:{port}{stream_path}"

    cmd = [
        "ffmpeg",
        "-y",
        "-rtsp_transport", "tcp",
        "-i", live_url,
        "-vframes", "1",
        "-q:v", "2",
        "-f", "image2",
        "-"
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=4)
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
    direction = request.args.get('direction', 'stop').lower()
    speed = float(request.args.get('speed', 0.4))
    ptz = get_ptz()
    ok, msg = ptz.move(direction=direction, speed=speed)
    return jsonify({"success": ok, "message": msg, "direction": direction})

@app.route('/api/ptz/stop', methods=['POST'])
def api_ptz_stop():
    ptz = get_ptz()
    ok, msg = ptz.stop()
    return jsonify({"success": ok, "message": msg})

# ---------------------------------------------------------------------------
# Settings & Control
# ---------------------------------------------------------------------------
@app.route('/settings', methods=['POST'])
def save_settings():
    curr_cfg = load_config()
    cam = curr_cfg.get('camera', {})

    cam['name'] = request.form.get('name', 'Gang Sebelah Timur').strip() or 'Gang Sebelah Timur'
    cam['ip'] = request.form.get('ip', '192.168.1.3').strip()
    cam['port'] = int(request.form.get('port', 554) or 554)
    cam['username'] = request.form.get('username', 'admin').strip()
    
    new_pw = request.form.get('password', '').strip()
    if new_pw:
        cam['password'] = new_pw

    cam['rtsp_path'] = request.form.get('rtsp_path', '/V_ENC_000').strip()
    cam['custom_rtsp_url'] = request.form.get('custom_rtsp_url', '').strip()
    cam['segment_seconds'] = int(request.form.get('segment_seconds', 3600) or 3600)
    
    # Guaranteed 24/7 Autonomous Auto-Record: ALWAYS enabled upon saving device
    cam['enabled'] = True

    ret_days = int(request.form.get('retention_days', 30) or 30)
    curr_cfg['retention_days'] = ret_days
    curr_cfg['camera'] = cam
    save_config(curr_cfg)

    # Immediately ensure recorder is running 24/7
    recorder.restart()
    flash(f'Pengaturan kamera "{cam["name"]}" berhasil disimpan. Perekam 24/7 aktif & merekam!', 'success')

    return redirect(url_for('index'))

@app.route('/recorder/start', methods=['POST'])
def recorder_start():
    curr_cfg = load_config()
    curr_cfg.setdefault('camera', {})['enabled'] = True
    save_config(curr_cfg)
    recorder.start()
    flash('Perekaman otomatis 24/7 dijalankan.', 'success')
    return redirect(url_for('index'))

@app.route('/recorder/stop', methods=['POST'])
def recorder_stop():
    curr_cfg = load_config()
    curr_cfg.setdefault('camera', {})['enabled'] = False
    save_config(curr_cfg)
    recorder.stop()
    flash('Perekaman dihentikan.', 'info')
    return redirect(url_for('index'))

@app.route('/recorder/test', methods=['POST'])
def recorder_test():
    curr_cfg = load_config()
    cam = dict(curr_cfg.get('camera', {}))
    if request.is_json:
        data = request.get_json()
        cam.update(data)
    ok, msg = recorder.test_connection(cam)
    return jsonify({"success": ok, "message": msg})

@app.route('/recorder/status')
def recorder_status_api():
    return jsonify(recorder.get_status())

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
