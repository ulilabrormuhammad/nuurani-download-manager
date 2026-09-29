import hashlib, os, re, shutil, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nuurani_dm.engine import Download, Status, RateLimiter

DATA = os.urandom(12 * 1024 * 1024 + 12345)
MD5 = hashlib.md5(DATA).hexdigest()
STATS = {"conns": set(), "reqs": 0}

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_GET(self):
        STATS["reqs"] += 1; STATS["conns"].add(id(self.connection))
        norange = self.path.startswith("/norange")
        rng = self.headers.get("Range")
        if rng and not norange:
            s, e = re.match(r"bytes=(\d+)-(\d*)", rng).groups()
            s = int(s); e = int(e) if e else len(DATA) - 1
            e = min(e, len(DATA) - 1); body = DATA[s:e + 1]
            self.send_response(206); self.send_header("Content-Range", f"bytes {s}-{e}/{len(DATA)}")
        else:
            body = DATA; self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", 'attachment; filename="tes file.bin"')
        self.end_headers()
        for i in range(0, len(body), 64 * 1024):  # ~ 6 MB/s per koneksi
            try: self.wfile.write(body[i:i + 64 * 1024])
            except Exception: return
            time.sleep(0.01)

srv = ThreadingHTTPServer(("127.0.0.1", 0), H); srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"
tmp = tempfile.mkdtemp()
md5 = lambda p: hashlib.md5(open(p, "rb").read()).hexdigest()

# 1) multi-koneksi + dynamic segmentation
t = time.time(); d = Download(base + "/file", tmp, connections=8, categorize=False); d.start(); d.wait()
print("1 multi:", d.status, d.filename, f"{time.time()-t:.2f}s", "segmen:", len(d.segments),
      "koneksi TCP:", len(STATS["conns"]), "request:", STATS["reqs"], "md5 ok:", md5(d.path) == MD5)
assert d.status == Status.COMPLETED and md5(d.path) == MD5

# 2) 1 koneksi sebagai pembanding
t = time.time(); d1 = Download(base + "/file", tmp, "single.bin", connections=1, categorize=False); d1.start(); d1.wait()
print("2 single:", d1.status, f"{time.time()-t:.2f}s", md5(d1.path) == MD5)

# 3) pause lalu resume dari state file (simulasi restart aplikasi)
d2 = Download(base + "/file", tmp, "resume.bin", connections=6, limiter=RateLimiter(3*1024*1024), categorize=False); d2.start()
time.sleep(0.8); d2.pause(); d2.wait()
got = d2.downloaded; print("3 paused:", d2.status, got, "state ada:", os.path.exists(d2.state_path))
d3 = Download.from_dict(d2.to_dict()); d3.status = Status.QUEUED; d3.start(); d3.wait()
print("3 resumed:", d3.status, "md5 ok:", md5(d3.path) == MD5, "state dihapus:", not os.path.exists(d3.state_path))
assert md5(d3.path) == MD5 and 0 < got < len(DATA)

# 4) server tanpa Range -> fallback 1 stream
d4 = Download(base + "/norange", tmp, "norange.bin", connections=8, categorize=False); d4.start(); d4.wait()
print("4 norange:", d4.status, "resumable:", d4.resumable, md5(d4.path) == MD5)
assert md5(d4.path) == MD5

# 5) nama duplikat -> "tes file (1).bin" + kategori
d5 = Download(base + "/file", tmp, connections=4, categorize=False); d5.start(); d5.wait()
print("5 dup name:", d5.filename)

# 6) speed limit 2 MB/s
t = time.time(); d6 = Download(base + "/file", tmp, "limit.bin", connections=8, limiter=RateLimiter(4*1024*1024), categorize=False); d6.start(); d6.wait()
print(f"6 limit 4MB/s: {time.time()-t:.2f}s (harapan ~3s)", md5(d6.path) == MD5)
print(sorted(os.listdir(tmp)))
shutil.rmtree(tmp)
