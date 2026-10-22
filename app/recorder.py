import os
import time
os.environ['TZ'] = 'Asia/Jakarta'
if hasattr(time, 'tzset'):
    time.tzset()
import subprocess
import threading
import logging

logger = logging.getLogger("cctv_recorder")

class CameraRecorder:
    def __init__(self, config_getter):
        self.config_getter = config_getter
        self.process = None
        self.thread = None
        self.stop_event = threading.Event()
        self.status = "idle"  # idle, recording, error, connecting
        self.last_error = ""
        self.start_time = None
        self.lock = threading.Lock()
        self.log_file = "/tmp/ffmpeg_recorder.log"
        self.last_healthy = time.time()

    def build_rtsp_url(self, cam_cfg):
        custom = cam_cfg.get("custom_rtsp_url", "").strip()
        if custom:
            return custom
        ip = cam_cfg.get("ip", "192.168.1.3").strip()
        port = cam_cfg.get("port", 554)
        user = cam_cfg.get("username", "admin").strip()
        password = cam_cfg.get("password", "admin").strip()
        path = cam_cfg.get("rtsp_path", "/V_ENC_000").strip()
        if not path.startswith("/"):
            path = "/" + path

        if user and password:
            return f"rtsp://{user}:{password}@{ip}:{port}{path}"
        elif user:
            return f"rtsp://{user}@{ip}:{port}{path}"
        else:
            return f"rtsp://{ip}:{port}{path}"

    def get_masked_url(self, cam_cfg):
        url = self.build_rtsp_url(cam_cfg)
        if "@" in url:
            prefix, rest = url.split("@", 1)
            scheme_cred = prefix.split("://")
            scheme = scheme_cred[0]
            return f"{scheme}://***:***@{rest}"
        return url

    def test_connection(self, cam_cfg):
        url = self.build_rtsp_url(cam_cfg)
        if not url:
            return False, "Camera IP belum diisi."
        
        cmd = [
            "ffmpeg",
            "-rtsp_transport", "tcp",
            "-timeout", "5000000",
            "-i", url,
            "-t", "2",
            "-f", "null",
            "-"
        ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=8)
            if res.returncode == 0:
                return True, "Koneksi ke CCTV RTSP Bardi (2K H.265) Berhasil!"
            else:
                err_msg = res.stderr.decode(errors="replace")
                lines = [l for l in err_msg.splitlines() if l.strip()]
                short_err = lines[-1] if lines else "Gagal membaca RTSP stream."
                return False, f"FFmpeg error: {short_err}"
        except subprocess.TimeoutExpired:
            return False, "Timeout saat menghubungkan ke IP CCTV. Pastikan IP dan Port 554 bisa dijangkau."
        except Exception as e:
            return False, f"Error: {str(e)}"

    def is_alive(self):
        return self.thread is not None and self.thread.is_alive() and not self.stop_event.is_set()

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return True, "Recorder sudah berjalan."
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._run_loop, daemon=True, name="cctv_watchdog")
            self.thread.start()
            return True, "Recorder dimulai."

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
            return True, "Recorder dihentikan."

    def restart(self):
        self.stop()
        return self.start()

    def _run_loop(self):
        """Continuous Watchdog Loop: Guarantees 24/7 non-stop auto-recording."""
        logger.info("Watchdog loop started.")
        while not self.stop_event.is_set():
            cfg = self.config_getter()
            cam_cfg = cfg.get("camera", {})
            if not cam_cfg.get("enabled", False):
                self.status = "disabled"
                time.sleep(3)
                continue

            rtsp_url = self.build_rtsp_url(cam_cfg)
            if not rtsp_url:
                self.status = "error"
                self.last_error = "IP CCTV belum dikonfigurasi."
                time.sleep(5)
                continue

            storage_path = cfg.get("storage_path", "/recordings")
            os.makedirs(storage_path, exist_ok=True)
            segment_sec = cam_cfg.get("segment_seconds", 3600)  # Default 1 hour

            output_pattern = os.path.join(storage_path, "bardi_%Y-%m-%d_%H-%M-%S.mp4")

            # Maximize 2K Video Quality: Pure bit-for-bit stream copy from sensor
            # Fragmented MP4 with clock-time alignment (rolls over on the hour!)
            cmd = [
                "ffmpeg",
                "-y",
                "-rtsp_transport", "tcp",
                "-timeout", "10000000",         # 10s socket timeout
                "-hide_banner",
                "-loglevel", "warning",
                "-avoid_negative_ts", "make_zero",
                "-fflags", "+genpts+discardcorrupt",
                "-i", rtsp_url,
                "-c:v", "copy",                  # 100% Raw 2K Super HD quality
                "-tag:v", "hvc1",
                "-c:a", "aac",                   # Universal audio
                "-b:a", "64k",
                "-ar", "8000",
                "-f", "segment",
                "-segment_time", str(segment_sec),
                "-segment_atclocktime", "1",     # Split exactly on hour clock (e.g. 01:00, 02:00)
                "-segment_format", "mp4",
                "-segment_format_options", "movflags=+faststart+frag_keyframe+empty_moov+default_base_moof",
                "-reset_timestamps", "1",
                "-strftime", "1",
                output_pattern
            ]

            self.status = "connecting"
            logger.info(f"Starting 24/7 NVR recording from {self.get_masked_url(cam_cfg)}")
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
                        logger.warning(f"Recorder dropped: {self.last_error}. Auto-reconnecting in 3s...")
            except Exception as e:
                self.status = "error"
                self.last_error = str(e)
                logger.error(f"Recorder exception: {e}")
            finally:
                self.process = None

            if not self.stop_event.is_set():
                # Auto-heal: quick pause and reconnect immediately
                time.sleep(3)

        self.status = "idle"

    def get_status(self):
        cfg = self.config_getter()
        cam_cfg = cfg.get("camera", {})
        uptime_str = "-"
        if self.start_time and self.status == "recording":
            secs = int(time.time() - self.start_time)
            hrs = secs // 3600
            mins = (secs % 3600) // 60
            secs = secs % 60
            uptime_str = f"{hrs:02d}:{mins:02d}:{secs:02d}"

        return {
            "status": self.status,
            "running": self.status == "recording",
            "uptime": uptime_str,
            "camera_ip": cam_cfg.get("ip", "192.168.1.3"),
            "camera_port": cam_cfg.get("port", 554),
            "camera_user": cam_cfg.get("username", "admin"),
            "rtsp_path": cam_cfg.get("rtsp_path", "/V_ENC_000"),
            "segment_seconds": cam_cfg.get("segment_seconds", 3600),
            "enabled": cam_cfg.get("enabled", True),
            "masked_url": self.get_masked_url(cam_cfg),
            "last_error": self.last_error,
        }
