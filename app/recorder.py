import os
import time
os.environ['TZ'] = 'Asia/Jakarta'
if hasattr(time, 'tzset'):
    time.tzset()
import subprocess
import threading
import logging

logger = logging.getLogger("cctv_recorder")

def build_stream_url(dev_cfg):
    dev_type = dev_cfg.get("type", "rtsp").lower()
    if dev_type == "hls":
        return dev_cfg.get("url", "").strip()

    custom = dev_cfg.get("custom_rtsp_url", "").strip()
    if custom:
        return custom
    ip = dev_cfg.get("ip", "192.168.1.3").strip()
    port = dev_cfg.get("port", 554)
    user = dev_cfg.get("username", "admin").strip()
    password = dev_cfg.get("password", "admin").strip()
    path = dev_cfg.get("rtsp_path", "/V_ENC_000").strip()
    if not path.startswith("/"):
        path = "/" + path

    if user and password:
        return f"rtsp://{user}:{password}@{ip}:{port}{path}"
    elif user:
        return f"rtsp://{user}@{ip}:{port}{path}"
    else:
        return f"rtsp://{ip}:{port}{path}"

def get_masked_url(dev_cfg):
    url = build_stream_url(dev_cfg)
    if "@" in url:
        prefix, rest = url.split("@", 1)
        scheme_cred = prefix.split("://")
        scheme = scheme_cred[0]
        return f"{scheme}://***:***@{rest}"
    return url

class DeviceRecorderWorker:
    """Worker independen per kamera: menjamin perekaman 24/7 jika record: True."""
    def __init__(self, dev_cfg, storage_path="/recordings"):
        self.dev_cfg = dict(dev_cfg)
        self.dev_id = dev_cfg.get("id", "bardi")
        self.storage_path = storage_path
        self.process = None
        self.thread = None
        self.stop_event = threading.Event()
        self.status = "idle"  # idle, recording, error, connecting
        self.last_error = ""
        self.start_time = None
        self.lock = threading.Lock()
        self.log_file = f"/tmp/ffmpeg_recorder_{self.dev_id}.log"

    def is_running(self):
        return self.thread is not None and self.thread.is_alive() and not self.stop_event.is_set()

    def start(self):
        with self.lock:
            if self.is_running():
                return True
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._run_loop, daemon=True, name=f"rec_{self.dev_id}")
            self.thread.start()
            return True

    def stop(self):
        with self.lock:
            self.stop_event.set()
            proc = self.process
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                self.process = None

            if self.thread and self.thread.is_alive() and threading.current_thread() != self.thread:
                try:
                    self.thread.join(timeout=4)
                except Exception:
                    pass
            self.thread = None
            self.status = "idle"
            self.start_time = None
            return True

    def _run_loop(self):
        logger.info(f"Recorder worker started for device [{self.dev_id}] {self.dev_cfg.get('name')}")
        while not self.stop_event.is_set():
            if not self.dev_cfg.get("enabled", True) or not self.dev_cfg.get("record", False):
                self.status = "idle"
                time.sleep(3)
                break

            stream_url = build_stream_url(self.dev_cfg)
            if not stream_url:
                self.status = "error"
                self.last_error = "URL / IP Kamera belum dikonfigurasi."
                time.sleep(5)
                continue

            os.makedirs(self.storage_path, exist_ok=True)
            segment_sec = int(self.dev_cfg.get("segment_seconds", 3600) or 3600)

            # Output pattern: keep bardi prefix for bardi, <dev_id> prefix for others
            prefix = "bardi" if self.dev_id == "bardi" else self.dev_id
            output_pattern = os.path.join(self.storage_path, f"{prefix}_%Y-%m-%d_%H-%M-%S.mp4")

            dev_type = self.dev_cfg.get("type", "rtsp").lower()
            if dev_type == "hls":
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel", "warning",
                    "-avoid_negative_ts", "make_zero",
                    "-fflags", "+genpts+discardcorrupt",
                    "-i", stream_url,
                    "-c", "copy",
                    "-f", "segment",
                    "-segment_time", str(segment_sec),
                    "-segment_atclocktime", "1",
                    "-segment_format", "mp4",
                    "-segment_format_options", "movflags=+faststart+frag_keyframe+empty_moov+default_base_moof",
                    "-reset_timestamps", "1",
                    "-strftime", "1",
                    output_pattern
                ]
            else:
                # RTSP stream (Bardi 2K)
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-rtsp_transport", "tcp",
                    "-timeout", "10000000",
                    "-hide_banner",
                    "-loglevel", "warning",
                    "-avoid_negative_ts", "make_zero",
                    "-fflags", "+genpts+discardcorrupt",
                    "-i", stream_url,
                    "-c:v", "copy",
                    "-tag:v", "hvc1",
                    "-c:a", "aac",
                    "-b:a", "64k",
                    "-ar", "8000",
                    "-f", "segment",
                    "-segment_time", str(segment_sec),
                    "-segment_atclocktime", "1",
                    "-segment_format", "mp4",
                    "-segment_format_options", "movflags=+faststart+frag_keyframe+empty_moov+default_base_moof",
                    "-reset_timestamps", "1",
                    "-strftime", "1",
                    output_pattern
                ]

            self.status = "connecting"
            proc = None
            try:
                with open(self.log_file, "a") as log_f:
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=log_f
                    )
                    self.process = proc
                    self.status = "recording"
                    self.start_time = time.time()
                    self.last_error = ""

                    while not self.stop_event.is_set() and proc.poll() is None:
                        time.sleep(2)

                    if self.stop_event.is_set():
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                        except Exception:
                            try:
                                proc.kill()
                            except Exception:
                                pass
                        break

                    return_code = proc.returncode
                    if return_code != 0 and not self.stop_event.is_set():
                        self.status = "error"
                        if os.path.exists(self.log_file):
                            with open(self.log_file, "r") as r_log:
                                lines = r_log.readlines()
                                self.last_error = "".join(lines[-4:]) if lines else f"Exit code {return_code}"
                        else:
                            self.last_error = f"FFmpeg exit code {return_code}"
                        logger.warning(f"Device [{self.dev_id}] recorder dropped: {self.last_error}. Auto-reconnecting...")
            except Exception as e:
                self.status = "error"
                self.last_error = str(e)
                logger.error(f"Device [{self.dev_id}] recorder exception: {e}")
            finally:
                self.process = None

            if not self.stop_event.is_set():
                time.sleep(3)

        self.status = "idle"
        logger.info(f"Recorder worker ended for device [{self.dev_id}]")

    def get_status(self):
        uptime_str = "-"
        if self.start_time and self.status == "recording":
            secs = int(time.time() - self.start_time)
            hrs = secs // 3600
            mins = (secs % 3600) // 60
            secs = secs % 60
            uptime_str = f"{hrs:02d}:{mins:02d}:{secs:02d}"

        return {
            "id": self.dev_id,
            "name": self.dev_cfg.get("name", self.dev_id),
            "status": self.status,
            "running": self.status == "recording",
            "record": self.dev_cfg.get("record", False),
            "enabled": self.dev_cfg.get("enabled", True),
            "uptime": uptime_str,
            "type": self.dev_cfg.get("type", "rtsp"),
            "segment_seconds": int(self.dev_cfg.get("segment_seconds", 3600) or 3600),
            "masked_url": get_masked_url(self.dev_cfg),
            "last_error": self.last_error
        }

class CameraRecorder:
    """Manajer Perekam Multi-Kamera: mengatur worker rekaman per device."""
    def __init__(self, config_getter):
        self.config_getter = config_getter
        self.workers = {}  # { dev_id: DeviceRecorderWorker }
        self.lock = threading.Lock()

    def sync_workers(self):
        """Menyelaraskan worker perekam dengan daftar perangkat di config."""
        with self.lock:
            cfg = self.config_getter()
            storage_path = cfg.get("storage_path", "/recordings")
            devices = cfg.get("devices", [])
            active_ids = set()

            for dev in devices:
                dev_id = dev.get("id", "bardi")
                active_ids.add(dev_id)
                should_record = dev.get("enabled", True) and dev.get("record", False)

                if should_record:
                    worker = self.workers.get(dev_id)
                    if not worker or not worker.is_running():
                        if worker:
                            worker.stop()
                        worker = DeviceRecorderWorker(dev, storage_path=storage_path)
                        self.workers[dev_id] = worker
                        worker.start()
                        logger.info(f"Started 24/7 recording for device: {dev.get('name')} [{dev_id}]")
                    else:
                        # Update config in case segment time or stream url changed
                        worker.dev_cfg = dict(dev)
                else:
                    worker = self.workers.get(dev_id)
                    if worker and worker.is_running():
                        worker.stop()
                        logger.info(f"Stopped recording for device: {dev.get('name')} [{dev_id}] (record=False)")

            # Remove workers for removed devices
            for dev_id in list(self.workers.keys()):
                if dev_id not in active_ids:
                    self.workers[dev_id].stop()
                    del self.workers[dev_id]

    def start(self):
        self.sync_workers()
        return True, "Multi-device recorder disinkronkan dan dijalankan."

    def stop(self):
        with self.lock:
            for worker in self.workers.values():
                worker.stop()
            self.workers.clear()
            return True, "Semua perekam dihentikan."

    def restart(self):
        self.stop()
        return self.start()

    def is_alive(self, device_id=None):
        if device_id:
            worker = self.workers.get(device_id)
            return worker.is_running() if worker else False
        # If any configured recording device should run, check if its worker is alive
        cfg = self.config_getter()
        for dev in cfg.get("devices", []):
            if dev.get("enabled", True) and dev.get("record", False):
                dev_id = dev.get("id", "bardi")
                worker = self.workers.get(dev_id)
                if not worker or not worker.is_running():
                    return False
        return True

    def get_status(self, device_id=None):
        cfg = self.config_getter()
        devices = cfg.get("devices", [])
        statuses = {}

        for dev in devices:
            dev_id = dev.get("id", "bardi")
            worker = self.workers.get(dev_id)
            if worker:
                statuses[dev_id] = worker.get_status()
            else:
                statuses[dev_id] = {
                    "id": dev_id,
                    "name": dev.get("name", dev_id),
                    "status": "idle" if not dev.get("record", False) else "stopped",
                    "running": False,
                    "record": dev.get("record", False),
                    "enabled": dev.get("enabled", True),
                    "uptime": "-",
                    "type": dev.get("type", "rtsp"),
                    "segment_seconds": int(dev.get("segment_seconds", 3600) or 3600),
                    "masked_url": get_masked_url(dev),
                    "last_error": ""
                }

        # Primary / current device status for backward compatibility
        primary_id = device_id or (devices[0]["id"] if devices else "bardi")
        primary_status = statuses.get(primary_id, {
            "status": "idle",
            "running": False,
            "uptime": "-",
            "segment_seconds": 3600,
            "last_error": ""
        })

        res = dict(primary_status)
        res["devices"] = statuses
        return res

    def test_connection(self, dev_cfg):
        dev_type = dev_cfg.get("type", "rtsp").lower()
        stream_url = build_stream_url(dev_cfg)
        if not stream_url:
            return False, "URL / IP Kamera belum diisi."

        if dev_type == "hls":
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-i", stream_url, "-t", "2", "-f", "null", "-"]
        else:
            cmd = ["ffmpeg", "-rtsp_transport", "tcp", "-timeout", "5000000", "-i", stream_url, "-t", "2", "-f", "null", "-"]

        try:
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=8)
            if res.returncode == 0:
                name = dev_cfg.get("name", "Kamera")
                return True, f"Koneksi ke {name} Berhasil!"
            else:
                err_msg = res.stderr.decode(errors="replace")
                lines = [l for l in err_msg.splitlines() if l.strip()]
                short_err = lines[-1] if lines else "Gagal membaca stream."
                return False, f"FFmpeg error: {short_err}"
        except subprocess.TimeoutExpired:
            return False, "Timeout saat menghubungkan ke kamera. Pastikan URL/IP dapat dijangkau."
        except Exception as e:
            return False, f"Error: {str(e)}"
