"""
Nuurani DM — Download Engine
============================
Meniru cara kerja Internet Download Manager (IDM):

1. Probe      : kirim GET dengan Range bytes=0-0 untuk tahu ukuran file,
                dukungan resume (HTTP 206), nama file, dan URL akhir (redirect).
2. Dynamic segmentation (aturan "bagi dua"):
                download dimulai dengan 1 segmen utuh. Setiap kali ada koneksi
                baru / koneksi yang selesai, engine mencari segmen AKTIF dengan
                sisa terbesar lalu membelahnya jadi dua. Koneksi baru mengambil
                separuh belakang. Semua koneksi selalu sibuk sampai akhir.
3. Connection reuse : koneksi HTTP keep-alive dipakai ulang untuk segmen
                berikutnya tanpa handshake TCP/TLS baru.
4. Tanpa merging : tiap koneksi menulis langsung ke offset-nya di satu file
                .part (os.pwrite) — tidak perlu proses "assembling" di akhir.
5. Resume aman : posisi setiap segmen disimpan ke file .nuurani tiap 2 detik,
                sehingga aman dari pause, force-quit, atau mati listrik.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import unquote, urlsplit

CHUNK = 128 * 1024              # ukuran baca per iterasi
MIN_SPLIT = 512 * 1024          # segmen tidak dibelah jika sisa < 2 x MIN_SPLIT
STATE_EXT = ".nuurani"
PART_EXT = ".part"

DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")

CATEGORIES = {
    "Video": {"mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "3gp", "mpg", "mpeg", "ts"},
    "Musik": {"mp3", "wav", "flac", "aac", "m4a", "ogg", "wma", "opus"},
    "Dokumen": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "epub", "odt", "rtf", "pages", "numbers", "key"},
    "Arsip": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "tgz", "iso"},
    "Program": {"dmg", "pkg", "app", "exe", "msi", "apk", "deb", "rpm", "sh", "jar"},
    "Gambar": {"jpg", "jpeg", "png", "gif", "webp", "heic", "svg", "bmp", "tiff", "psd"},
}


def category_of(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return "Lainnya"


def ssl_context() -> ssl.SSLContext:
    # python.org Python di macOS sering tidak punya CA bundle -> pakai certifi
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_SSL = ssl_context()


def sanitize_filename(name: str) -> str:
    name = name.strip().replace("/", "_").replace(":", "_").replace("\x00", "")
    name = re.sub(r"[\r\n\t]", " ", name)
    return name[:240] or "download"


def filename_from_headers(cd: str | None, url: str) -> str:
    if cd:
        m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", cd, re.I)
        if m:
            return sanitize_filename(unquote(m.group(1).strip('"')))
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
        if m:
            return sanitize_filename(unquote(m.group(1)))
    path = urlsplit(url).path
    base = unquote(path.rsplit("/", 1)[-1]) if path else ""
    return sanitize_filename(base or "download")


def unique_path(path: str) -> str:
    if not (os.path.exists(path) or os.path.exists(path + PART_EXT)):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while True:
        cand = f"{root} ({i}){ext}"
        if not (os.path.exists(cand) or os.path.exists(cand + PART_EXT)):
            return cand
        i += 1


def human_size(n: float | int | None) -> str:
    if n is None or n < 0:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "?"


def human_time(sec: float | None) -> str:
    if sec is None or sec < 0 or sec == float("inf"):
        return "—"
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}j {m:02d}m" if h else (f"{m}m {s:02d}d" if m else f"{s}d")


class RateLimiter:
    """Token bucket global (dibagi semua koneksi). bps=0 berarti tanpa batas."""

    def __init__(self, bps: int = 0):
        self.bps = bps
        self._lock = threading.Lock()
        self._allow = 0.0
        self._last = time.monotonic()

    def set(self, bps: int):
        with self._lock:
            self.bps = max(0, int(bps))
            self._allow = 0.0
            self._last = time.monotonic()

    def consume(self, n: int):
        if self.bps <= 0:
            return
        with self._lock:
            now = time.monotonic()
            self._allow = min(self.bps, self._allow + (now - self._last) * self.bps)
            self._last = now
            self._allow -= n
            deficit = -self._allow
        if deficit > 0:
            time.sleep(min(deficit / self.bps, 2.0))


class Segment:
    __slots__ = ("start", "pos", "end", "active")

    def __init__(self, start: int, pos: int, end: int, active: bool = False):
        self.start, self.pos, self.end, self.active = start, pos, end, active

    @property
    def remaining(self) -> int:
        return max(0, self.end - self.pos + 1)


class Status:
    QUEUED = "Antre"
    CONNECTING = "Menghubungkan"
    DOWNLOADING = "Mengunduh"
    PAUSED = "Dijeda"
    COMPLETED = "Selesai"
    ERROR = "Gagal"


class Download:
    def __init__(self, url: str, folder: str, filename: str | None = None,
                 connections: int = 8, headers: dict | None = None,
                 limiter: RateLimiter | None = None, categorize: bool = True,
                 timeout: float = 20, max_retries: int = 8, on_finish=None,
                 id: str | None = None):
        self.id = id or uuid.uuid4().hex[:12]
        self.url = url
        self.final_url = url
        self.base_folder = folder
        self.folder = folder
        self.filename = filename
        self.user_filename = bool(filename)
        self.connections = max(1, min(32, int(connections)))
        self.headers = dict(headers or {})
        self.headers.setdefault("User-Agent", DEFAULT_UA)
        self.limiter = limiter or RateLimiter()
        self.categorize = categorize
        self.timeout = timeout
        self.max_retries = max_retries
        self.on_finish = on_finish

        self.size = -1
        self.resumable = False
        self.etag = None
        self.content_type = ""
        self.downloaded = 0
        self.status = Status.QUEUED
        self.error = ""
        self.added_at = time.time()
        self.finished_at = None
        self.speed = 0.0
        self.active_conns = 0

        self.segments: list[Segment] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._conns: set = set()
        self._fd = None
        self._samples: list[tuple[float, int]] = []
        self._last_err = ""
        self.claimed = False

    # ---------------------------------------------------------------- paths
    @property
    def category(self) -> str:
        return category_of(self.filename or self.url)

    @property
    def path(self) -> str:
        return os.path.join(self.folder, self.filename or "download")

    @property
    def part_path(self) -> str:
        return self.path + PART_EXT

    @property
    def state_path(self) -> str:
        return self.path + STATE_EXT

    @property
    def progress(self) -> float:
        if self.status == Status.COMPLETED:
            return 1.0
        return self.downloaded / self.size if self.size > 0 else 0.0

    @property
    def eta(self) -> float | None:
        if self.size <= 0 or self.speed <= 0:
            return None
        return (self.size - self.downloaded) / self.speed

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---------------------------------------------------------------- control
    def start(self):
        if self.is_running() or self.status == Status.COMPLETED:
            return
        self._stop.clear()
        self.error = ""
        self.status = Status.CONNECTING
        self._thread = threading.Thread(target=self._run, name=f"dl-{self.id}", daemon=True)
        self._thread.start()

    def pause(self):
        if self.status in (Status.QUEUED,):
            self.status = Status.PAUSED
            return
        self._stop.set()
        self._kill_conns()

    def wait(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    def _kill_conns(self):
        with self._lock:
            conns = list(self._conns)
        for c in conns:
            try:
                if c.sock:
                    c.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass

    # ---------------------------------------------------------------- network
    def _proxy_for(self, scheme: str):
        try:
            p = urllib.request.getproxies().get(scheme)
        except Exception:
            p = None
        if not p:
            return None
        try:
            if urllib.request.proxy_bypass(urlsplit(self.final_url).hostname or ""):
                return None
        except Exception:
            pass
        return urlsplit(p if "://" in p else "http://" + p)

    def _new_conn(self):
        u = urlsplit(self.final_url)
        proxy = self._proxy_for(u.scheme)
        port = u.port or (443 if u.scheme == "https" else 80)
        if proxy:
            if u.scheme == "https":
                c = http.client.HTTPSConnection(proxy.hostname, proxy.port or 8080,
                                                timeout=self.timeout, context=_SSL)
                c.set_tunnel(u.hostname, port)
            else:
                c = http.client.HTTPConnection(proxy.hostname, proxy.port or 8080, timeout=self.timeout)
            c._nuu_path = self.final_url if u.scheme == "http" else self._path(u)
        else:
            if u.scheme == "https":
                c = http.client.HTTPSConnection(u.hostname, port, timeout=self.timeout, context=_SSL)
            else:
                c = http.client.HTTPConnection(u.hostname, port, timeout=self.timeout)
            c._nuu_path = self._path(u)
        with self._lock:
            self._conns.add(c)
        return c

    @staticmethod
    def _path(u):
        p = u.path or "/"
        return p + ("?" + u.query if u.query else "")

    def _drop_conn(self, c):
        if c is None:
            return
        try:
            c.close()
        except Exception:
            pass
        with self._lock:
            self._conns.discard(c)

    def _probe(self):
        h = dict(self.headers)
        h["Range"] = "bytes=0-0"
        h["Accept-Encoding"] = "identity"
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_SSL))
        req = urllib.request.Request(self.url, headers=h)
        try:
            resp = opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            if e.code == 416:  # file kosong atau range ditolak -> coba tanpa Range
                h.pop("Range")
                resp = opener.open(urllib.request.Request(self.url, headers=h), timeout=self.timeout)
            else:
                raise
        try:
            self.final_url = resp.geturl()
            status = resp.status
            size, resumable = -1, False
            cr = resp.headers.get("Content-Range", "")
            if status == 206 and "/" in cr:
                total = cr.rsplit("/", 1)[-1].strip()
                if total.isdigit():
                    size, resumable = int(total), True
            if size < 0:
                cl = resp.headers.get("Content-Length")
                size = int(cl) if cl and cl.isdigit() and status == 200 else -1
            self.size = size
            self.resumable = resumable and size > 0
            self.etag = resp.headers.get("ETag")
            self.content_type = resp.headers.get("Content-Type", "")
            if not self.filename:
                self.filename = filename_from_headers(resp.headers.get("Content-Disposition"), self.final_url)
                if "." not in self.filename and "html" in self.content_type:
                    self.filename += ".html"
        finally:
            resp.close()

    # ---------------------------------------------------------------- state
    def _load_state(self) -> bool:
        try:
            with open(self.state_path) as f:
                st = json.load(f)
            if st.get("size") != self.size or (self.etag and st.get("etag") and st["etag"] != self.etag):
                return False  # file di server berubah -> mulai ulang
            if not os.path.exists(self.part_path):
                return False
            self.segments = [Segment(s, p, e) for s, p, e in st["segments"]]
            return True
        except Exception:
            return False

    def save_state(self):
        if not self.resumable or not self.segments:
            return
        with self._lock:
            data = {"url": self.url, "final_url": self.final_url, "size": self.size, "etag": self.etag,
                    "segments": [[s.start, s.pos, s.end] for s in self.segments]}
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    # ---------------------------------------------------------------- segmentation
    def _acquire(self) -> Segment | None:
        """Ambil segmen yang belum dikerjakan; kalau tidak ada, belah segmen aktif terbesar."""
        with self._lock:
            idle = [s for s in self.segments if not s.active and s.remaining > 0]
            if idle:
                s = min(idle, key=lambda x: x.pos)
                s.active = True
                return s
            busy = [s for s in self.segments if s.active and s.remaining >= 2 * MIN_SPLIT]
            if not busy:
                return None
            big = max(busy, key=lambda x: x.remaining)
            mid = big.pos + big.remaining // 2
            new = Segment(mid, mid, big.end, active=True)
            big.end = mid - 1
            self.segments.append(new)
            return new

    def _fetch_range(self, conn, seg: Segment) -> bool:
        """Unduh satu segmen. Return True bila koneksi masih bisa dipakai ulang."""
        with self._lock:
            start, end = seg.pos, seg.end
        h = dict(self.headers)
        h["Range"] = f"bytes={start}-{end}"
        h["Accept-Encoding"] = "identity"
        h["Connection"] = "keep-alive"
        conn.request("GET", conn._nuu_path, headers=h)
        r = conn.getresponse()
        if r.status != 206:
            r.close()
            raise IOError(f"HTTP {r.status} untuk range {start}-{end}")
        cr = r.getheader("Content-Range", "")
        if not cr.startswith(f"bytes {start}-"):
            r.close()
            raise IOError(f"Content-Range tidak cocok: {cr}")
        while True:
            if self._stop.is_set():
                return False
            buf = r.read(CHUNK)
            if not buf:
                break
            self.limiter.consume(len(buf))
            with self._lock:
                n = min(len(buf), seg.end - seg.pos + 1)
                off = seg.pos
            if n > 0:
                os.pwrite(self._fd, buf if n == len(buf) else buf[:n], off)
                with self._lock:
                    seg.pos += n
                    self.downloaded += n
            if seg.pos > seg.end:  # segmen tuntas (mungkin karena dibelah)
                clean = (n == len(buf) and r.length == 0)
                if clean:
                    r.read()
                return clean
        with self._lock:
            if seg.pos <= seg.end:
                raise IOError("koneksi terputus sebelum segmen selesai")
        return True

    def _worker(self):
        conn, retries = None, 0
        with self._lock:
            self.active_conns += 1
        try:
            while not self._stop.is_set():
                seg = self._acquire()
                if seg is None:
                    break
                try:
                    if conn is None:
                        conn = self._new_conn()
                    reusable = self._fetch_range(conn, seg)
                    retries = 0
                    if not reusable:
                        self._drop_conn(conn)
                        conn = None
                except Exception as e:  # noqa: BLE001
                    self._drop_conn(conn)
                    conn = None
                    if self._stop.is_set():
                        break
                    retries += 1
                    self._last_err = str(e)
                    if retries > self.max_retries:
                        break
                    self._stop.wait(min(20, 0.5 * 2 ** retries))
                finally:
                    with self._lock:
                        seg.active = False
        finally:
            self._drop_conn(conn)
            with self._lock:
                self.active_conns -= 1

    def _single_stream(self):
        """Fallback bila server tidak mendukung Range: 1 koneksi, tanpa resume."""
        h = dict(self.headers)
        h["Accept-Encoding"] = "identity"
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_SSL))
        self.downloaded = 0
        with self._lock:
            self.active_conns = 1
        try:
            with opener.open(urllib.request.Request(self.final_url, headers=h), timeout=self.timeout) as r:
                off = 0
                while not self._stop.is_set():
                    buf = r.read(CHUNK)
                    if not buf:
                        break
                    self.limiter.consume(len(buf))
                    os.pwrite(self._fd, buf, off)
                    off += len(buf)
                    self.downloaded = off
            if not self._stop.is_set():
                self.size = self.downloaded
        finally:
            with self._lock:
                self.active_conns = 0

    # ---------------------------------------------------------------- main loop
    def _tick_speed(self):
        now = time.monotonic()
        self._samples.append((now, self.downloaded))
        self._samples = [s for s in self._samples if now - s[0] <= 3.0]
        if len(self._samples) >= 2:
            dt = self._samples[-1][0] - self._samples[0][0]
            db = self._samples[-1][1] - self._samples[0][1]
            self.speed = db / dt if dt > 0 else 0.0

    def _run(self):
        try:
            self._probe()
            if self.categorize and not self.user_filename and self.folder == self.base_folder:
                self.folder = os.path.join(self.base_folder, self.category)
            os.makedirs(self.folder, exist_ok=True)

            resumed = self.resumable and self._load_state()
            if not resumed:
                if not self.claimed:  # pilih nama unik sekali saja, lalu "klaim"
                    self.filename = os.path.basename(unique_path(self.path))
                    self.claimed = True
                try:
                    os.remove(self.state_path)
                except OSError:
                    pass
                self.segments = [Segment(0, 0, self.size - 1)] if self.resumable else []
                with open(self.part_path, "wb") as f:
                    if self.size > 0:
                        f.truncate(self.size)  # sparse file di APFS, instan
            self.downloaded = sum(s.pos - s.start for s in self.segments) if self.resumable else 0
            self._fd = os.open(self.part_path, os.O_RDWR)
            self.status = Status.DOWNLOADING
            self._samples.clear()

            if self.resumable:
                workers = []
                for i in range(self.connections):
                    if self._stop.is_set():
                        break
                    t = threading.Thread(target=self._worker, daemon=True, name=f"{self.id}-w{i}")
                    t.start()
                    workers.append(t)
                    self._stop.wait(0.15)  # buka koneksi bertahap seperti IDM
                last_save = time.monotonic()
                while any(t.is_alive() for t in workers):
                    self._stop.wait(0.5)
                    self._tick_speed()
                    if time.monotonic() - last_save > 2:
                        self.save_state()
                        last_save = time.monotonic()
                for t in workers:
                    t.join()
                done = all(s.remaining == 0 for s in self.segments)
            else:
                t = threading.Thread(target=self._single_stream, daemon=True)
                t.start()
                while t.is_alive():
                    t.join(0.5)
                    self._tick_speed()
                done = not self._stop.is_set()

            os.close(self._fd)
            self._fd = None
            self.speed = 0.0
            if done:
                self._finalize()
            elif self._stop.is_set():
                self.save_state()
                self.status = Status.PAUSED
            else:
                self.save_state()
                self.status = Status.ERROR
                self.error = self._last_err or "Semua koneksi gagal"
        except Exception as e:  # noqa: BLE001
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
            self.speed = 0.0
            if self._stop.is_set():
                self.status = Status.PAUSED
            else:
                self.status = Status.ERROR
                self.error = str(e)
        finally:
            if self.on_finish:
                try:
                    self.on_finish(self)
                except Exception:
                    pass

    def _finalize(self):
        part, state, target = self.part_path, self.state_path, self.path
        if os.path.exists(target):
            target = unique_path(target)
        os.replace(part, target)
        try:
            os.remove(state)
        except OSError:
            pass
        self.filename = os.path.basename(target)
        if self.size <= 0:
            self.size = self.downloaded
        self.downloaded = self.size
        self.status = Status.COMPLETED
        self.finished_at = time.time()

    # ---------------------------------------------------------------- serialisasi
    def snapshot(self) -> dict:
        with self._lock:
            segs = [(s.start, s.pos, s.end) for s in self.segments]
        return {
            "id": self.id, "url": self.url, "filename": self.filename or urlsplit(self.url).path.rsplit("/", 1)[-1] or self.url,
            "path": self.path, "folder": self.folder, "size": self.size, "downloaded": self.downloaded,
            "progress": self.progress, "speed": self.speed, "eta": self.eta, "status": self.status,
            "error": self.error, "category": self.category, "connections": self.connections,
            "active_conns": self.active_conns, "resumable": self.resumable, "segments": segs,
            "added_at": self.added_at, "finished_at": self.finished_at,
        }

    def to_dict(self) -> dict:
        return {"id": self.id, "url": self.url, "final_url": self.final_url, "base_folder": self.base_folder,
                "folder": self.folder, "filename": self.filename, "user_filename": self.user_filename,
                "connections": self.connections, "headers": self.headers, "size": self.size,
                "downloaded": self.downloaded, "status": self.status, "error": self.error,
                "added_at": self.added_at, "finished_at": self.finished_at, "resumable": self.resumable,
                "categorize": self.categorize, "claimed": self.claimed}

    @classmethod
    def from_dict(cls, d: dict, **kw) -> "Download":
        dl = cls(d["url"], d["base_folder"], d.get("filename"), d.get("connections", 8),
                 d.get("headers"), id=d["id"], categorize=d.get("categorize", True), **kw)
        dl.user_filename = d.get("user_filename", False)
        dl.folder = d.get("folder", dl.base_folder)
        for k in ("final_url", "size", "downloaded", "error", "added_at", "finished_at", "resumable", "claimed"):
            if k in d:
                setattr(dl, k, d[k])
        st = d.get("status", Status.PAUSED)
        dl.status = st if st in (Status.COMPLETED, Status.ERROR, Status.QUEUED) else Status.PAUSED
        if dl.resumable and st != Status.COMPLETED:
            try:
                with open(dl.state_path) as f:
                    dl.segments = [Segment(s, p, e) for s, p, e in json.load(f)["segments"]]
            except Exception:
                pass
        return dl
