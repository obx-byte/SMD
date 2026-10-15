"""Supervised worker for threshold-triggered storage transfer."""

import logging
import hashlib
import os
import tempfile
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings

from VisionInspection.models import StorageConfiguration
from VisionInspection.services.storage_service import automatic_transfer_batch

logger = logging.getLogger(__name__)


class BaseWorkerLock:
    def __init__(self):
        workspace_id = hashlib.sha256(str(settings.BASE_DIR).encode("utf-8")).hexdigest()[:12]
        self.path = Path(tempfile.gettempdir()) / f"smd-vision-storage-worker-{workspace_id}.lock"
        self.fd = None

    @staticmethod
    def _pid_is_running(pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True

    def acquire(self):
        for _attempt in range(2):
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(self.fd, str(os.getpid()).encode("ascii"))
                return
            except FileExistsError:
                try:
                    pid = int(self.path.read_text(encoding="ascii").strip())
                except (OSError, ValueError):
                    pid = 0
                if pid and self._pid_is_running(pid):
                    raise CommandError(f"Storage worker is already running as PID {pid}.")
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
        raise CommandError("Could not acquire the storage worker lock.")

    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class Command(BaseCommand):
    help = "Run the supervised, resumable automatic image storage transfer worker."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=None, help="Override the configured idle check interval in seconds.")

    def handle(self, *args, **options):
        lock = BaseWorkerLock()
        lock.acquire()
        config = StorageConfiguration.get_config()
        interval = max(2, min(int(options["interval"] or config.monitoring_interval_seconds or 60), 300))
        self.stdout.write("Automatic storage worker started.")
        try:
            while True:
                try:
                    result = automatic_transfer_batch()
                    if result.get("started"):
                        self.stdout.write(
                            f"Transfer batch: {result.get('success', 0)} copied, "
                            f"{result.get('failed', 0)} failed; usage {result.get('usage_percent', 'unknown')}%."
                        )
                        time.sleep(2)
                    else:
                        time.sleep(interval)
                except Exception:
                    logger.exception("Automatic storage transfer batch failed")
                    time.sleep(interval)
        except KeyboardInterrupt:
            self.stdout.write("Automatic storage worker stopping.")
        finally:
            lock.release()
