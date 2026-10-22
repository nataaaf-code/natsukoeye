import os
import subprocess
import logging
import threading
import time

logger = logging.getLogger("cctv_gdrive")

class GDriveSync:
    def __init__(self, storage_path="/recordings", remote_name="gdrive:cctv_recordings"):
        self.storage_path = storage_path
        self.remote_name = remote_name
        self.running = False
        self.thread = None
        self.last_sync = "-"
        self.last_status = "idle"

    def is_rclone_installed(self):
        return shutil_which("rclone") is not None

    def get_status(self):
        installed = shutil_which("rclone") is not None
        return {
            "installed": installed,
            "remote": self.remote_name,
            "last_sync": self.last_sync,
            "status": self.last_status,
        }

    def sync_once(self):
        if not shutil_which("rclone"):
            return False, "rclone belum terinstal di host/container"
        
        # Only sync completed files (older than 2 minutes)
        cmd = [
            "rclone", "copy",
            "--min-age", "2m",
            "--transfers", "2",
            self.storage_path,
            self.remote_name
        ]
        try:
            self.last_status = "syncing"
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
            if res.returncode == 0:
                self.last_sync = time.strftime("%Y-%m-%d %H:%M:%S")
                self.last_status = "synced"
                return True, "Sync to Google Drive completed."
            else:
                err = res.stderr.decode(errors="replace")
                self.last_status = "error"
                return False, f"Rclone error: {err[:200]}"
        except Exception as e:
            self.last_status = "error"
            return False, str(e)

def shutil_which(cmd):
    for path in os.environ.get("PATH", "").split(os.pathsep):
        full = os.path.join(path, cmd)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            return full
    return None
