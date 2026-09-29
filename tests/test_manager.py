import json, os, sys, tempfile, time, urllib.request, threading, re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nuurani_dm.manager import Manager
from nuurani_dm.server import start_server, item_headers
DATA = os.urandom(3*1024*1024)
class H(BaseHTTPRequestHandler):
    protocol_version="HTTP/1.1"
    def log_message(self,*a):pass
    def do_GET(self):
        assert self.headers.get("Referer")=="https://contoh.id/" or "nocheck" in self.path
        s,e=map(int,re.match(r"bytes=(\d+)-(\d+)",self.headers["Range"]).groups()); b=DATA[s:e+1]
        self.send_response(206); self.send_header("Content-Range",f"bytes {s}-{e}/{len(DATA)}"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
srv=ThreadingHTTPServer(("127.0.0.1",0),H); threading.Thread(target=srv.serve_forever,daemon=True).start()
tmp=tempfile.mkdtemp(); m=Manager(tmp); m.settings.download_dir=os.path.join(tmp,"dl"); m.settings.max_concurrent=2
got=[]
start_server(52790, lambda it: got.append(m.add(it["url"], headers=item_headers(it))))
for n in ["video.mp4","lagu.mp3","doc.pdf","arsip.zip"]:
    body=json.dumps({"url":f"http://127.0.0.1:{srv.server_port}/{n}","referrer":"https://contoh.id/"}).encode()
    print(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:52790/add",body,{"Content-Type":"application/json"})).read())
time.sleep(0.2); print("berjalan (maks 2):", m.running_count())
while any(d.status!="Selesai" for d in m.downloads.values()): time.sleep(0.2)
for s in m.snapshots(): print(s["status"], s["category"], os.path.relpath(s["path"],tmp))
m2=Manager(tmp); print("reload:", len(m2.downloads))
