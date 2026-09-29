"""Server lokal 127.0.0.1 untuk menerima tautan dari ekstensi browser (pengganti IDM Integration Module)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    callback = None

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/ping"):
            self._reply(200, {"app": "Nuurani DM", "ok": True})
        else:
            self._reply(404, {"ok": False})

    def do_POST(self):
        if not self.path.startswith("/add"):
            return self._reply(404, {"ok": False})
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(min(n, 1_000_000)) or b"{}")
            items = data.get("items") or [data]
            for it in items:
                url = str(it.get("url", ""))
                if url.startswith(("http://", "https://")) and self.callback:
                    self.callback(it)
            self._reply(200, {"ok": True, "count": len(items)})
        except Exception as e:  # noqa: BLE001
            self._reply(400, {"ok": False, "error": str(e)})

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def start_server(port: int, callback) -> ThreadingHTTPServer | None:
    """callback(item: dict) dengan kunci url, referrer, cookies, filename."""
    handler = type("H", (_Handler,), {"callback": staticmethod(callback)})
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError:
        return None  # port dipakai (mungkin instance lain berjalan)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="nuurani-server").start()
    return srv


def item_headers(item: dict) -> dict:
    h = {}
    if item.get("referrer"):
        h["Referer"] = item["referrer"]
    if item.get("cookies"):
        h["Cookie"] = item["cookies"]
    if item.get("userAgent"):
        h["User-Agent"] = item["userAgent"]
    return h
