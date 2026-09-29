"""Antrean, pengaturan, persistensi, dan integrasi macOS."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass, fields

from .engine import Download, RateLimiter, Status

IS_MAC = sys.platform == "darwin"


def data_dir() -> str:
    if IS_MAC:
        d = os.path.expanduser("~/Library/Application Support/Nuurani DM")
    else:
        d = os.path.expanduser("~/.nuurani-dm")
    os.makedirs(d, exist_ok=True)
    return d


@dataclass
class Settings:
    download_dir: str = os.path.expanduser("~/Downloads/Nuurani")
    connections: int = 8            # koneksi per unduhan (IDM default 8)
    max_concurrent: int = 3         # unduhan berjalan bersamaan
    speed_limit_kbps: int = 0       # 0 = tanpa batas
    categorize: bool = True         # simpan ke subfolder Video/Musik/Dokumen/...
    clipboard_monitor: bool = True
    notify: bool = True
    prevent_sleep: bool = True      # caffeinate selama ada unduhan aktif
    server_port: int = 52789        # untuk ekstensi browser
    max_retries: int = 8
    timeout: int = 20

    @classmethod
    def load(cls, path: str) -> "Settings":
        s = cls()
        try:
            with open(path) as f:
                raw = json.load(f)
            names = {f.name for f in fields(cls)}
            for k, v in raw.items():
                if k in names:
                    setattr(s, k, v)
        except Exception:
            pass
        return s

    def save(self, path: str):
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)


def notify(title: str, message: str):
    if not IS_MAC:
        return
    esc = lambda t: t.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.Popen(["osascript", "-e",
                          f'display notification "{esc(message)}" with title "{esc(title)}" sound name "Glass"'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def reveal_in_finder(path: str):
    if IS_MAC:
        if os.path.exists(path):
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["open", os.path.dirname(path)])


def open_file(path: str):
    if IS_MAC and os.path.exists(path):
        subprocess.Popen(["open", path])


class Manager:
    def __init__(self, directory: str | None = None):
        self.dir = directory or data_dir()
        self.settings_path = os.path.join(self.dir, "settings.json")
        self.list_path = os.path.join(self.dir, "downloads.json")
        self.settings = Settings.load(self.settings_path)
        self.limiter = RateLimiter(self.settings.speed_limit_kbps * 1024)
        self.downloads: dict[str, Download] = {}
        self._lock = threading.RLock()
        self._caffeinate: subprocess.Popen | None = None
        self.on_event = None  # callback(kind, snapshot) — dipanggil dari thread engine
        self.load()

    # ------------------------------------------------------------ persistence
    def load(self):
        try:
            with open(self.list_path) as f:
                items = json.load(f)
        except Exception:
            items = []
        for d in items:
            try:
                dl = Download.from_dict(d, limiter=self.limiter, on_finish=self._on_finish,
                                        timeout=self.settings.timeout, max_retries=self.settings.max_retries)
                self.downloads[dl.id] = dl
            except Exception:
                continue

    def save(self):
        with self._lock:
            data = [d.to_dict() for d in self.downloads.values()]
        tmp = self.list_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.list_path)

    def apply_settings(self):
        self.settings.save(self.settings_path)
        self.limiter.set(self.settings.speed_limit_kbps * 1024)
        self._schedule()

    # ------------------------------------------------------------ operations
    def add(self, url: str, folder: str | None = None, filename: str | None = None,
            connections: int | None = None, headers: dict | None = None, start: bool = True) -> Download:
        folder = folder or self.settings.download_dir
        dl = Download(url.strip(), folder, filename or None, connections or self.settings.connections,
                      headers, limiter=self.limiter, categorize=self.settings.categorize and folder == self.settings.download_dir,
                      timeout=self.settings.timeout, max_retries=self.settings.max_retries,
                      on_finish=self._on_finish)
        if not start:
            dl.status = Status.PAUSED
        with self._lock:
            self.downloads[dl.id] = dl
        self.save()
        self._schedule()
        return dl

    def get(self, did: str):
        return self.downloads.get(did)

    def resume(self, did: str):
        dl = self.downloads.get(did)
        if dl and dl.status in (Status.PAUSED, Status.ERROR):
            dl.status = Status.QUEUED
            self._schedule()

    def pause(self, did: str):
        dl = self.downloads.get(did)
        if dl and dl.status not in (Status.COMPLETED, Status.ERROR):
            dl.pause()
            self.save()

    def resume_all(self):
        for d in list(self.downloads.values()):
            if d.status in (Status.PAUSED, Status.ERROR):
                d.status = Status.QUEUED
        self._schedule()

    def pause_all(self):
        for d in list(self.downloads.values()):
            if d.status not in (Status.COMPLETED, Status.ERROR):
                d.pause()
        self.save()

    def remove(self, did: str, delete_file: bool = False):
        dl = self.downloads.get(did)
        if not dl:
            return
        dl.pause()
        dl.wait(5)
        paths = [dl.part_path, dl.state_path]
        if delete_file and dl.status == Status.COMPLETED:
            paths.append(dl.path)
        if dl.status == Status.COMPLETED and not delete_file:
            paths = []
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass
        with self._lock:
            self.downloads.pop(did, None)
        self.save()

    def clear_completed(self):
        with self._lock:
            for k in [k for k, d in self.downloads.items() if d.status == Status.COMPLETED]:
                self.downloads.pop(k)
        self.save()

    def snapshots(self) -> list[dict]:
        with self._lock:
            return [d.snapshot() for d in self.downloads.values()]

    def total_speed(self) -> float:
        return sum(d.speed for d in list(self.downloads.values()))

    def running_count(self) -> int:
        return sum(1 for d in list(self.downloads.values()) if d.is_running())

    def shutdown(self):
        for d in list(self.downloads.values()):
            if d.is_running():
                d.pause()
        for d in list(self.downloads.values()):
            d.wait(5)
            d.save_state()
        self.save()
        self._sleep_guard(False)

    # ------------------------------------------------------------ scheduler
    def _schedule(self):
        with self._lock:
            running = sum(1 for d in self.downloads.values() if d.is_running())
            for d in self.downloads.values():
                if running >= self.settings.max_concurrent:
                    break
                if d.status == Status.QUEUED and not d.is_running():
                    d.start()
                    running += 1
        self._sleep_guard(running > 0)

    def _on_finish(self, dl: Download):
        self.save()
        if self.on_event:
            self.on_event(dl.status, dl.snapshot())
        if self.settings.notify:
            if dl.status == Status.COMPLETED:
                notify("Unduhan selesai", dl.filename or dl.url)
            elif dl.status == Status.ERROR:
                notify("Unduhan gagal", f"{dl.filename or dl.url}: {dl.error}")
        # jalankan antrean berikutnya di thread terpisah agar thread lama bisa selesai
        threading.Timer(0.05, self._schedule).start()

    def _sleep_guard(self, active: bool):
        """Cegah Mac tidur (idle sleep) selama ada unduhan — seperti opsi IDM."""
        if not IS_MAC or not self.settings.prevent_sleep:
            active = False
        if active and self._caffeinate is None:
            try:
                self._caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            except Exception:
                self._caffeinate = None
        elif not active and self._caffeinate is not None:
            self._caffeinate.terminate()
            self._caffeinate = None
