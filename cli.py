"""Mode baris perintah:  python -m nuurani_dm.cli URL [-c 16] [-o ~/Downloads]"""
import argparse
import os
import sys
import time

from .engine import Download, RateLimiter, Status, human_size, human_time


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nuurani-dm", description="Nuurani DM – pengunduh multi-koneksi")
    ap.add_argument("url")
    ap.add_argument("-o", "--output", default=os.path.expanduser("~/Downloads"))
    ap.add_argument("-n", "--name", default=None)
    ap.add_argument("-c", "--connections", type=int, default=8)
    ap.add_argument("-l", "--limit", type=int, default=0, help="batas kecepatan KB/s")
    a = ap.parse_args(argv)
    dl = Download(a.url, os.path.expanduser(a.output), a.name, a.connections,
                  limiter=RateLimiter(a.limit * 1024), categorize=False)
    dl.start()
    try:
        while dl.is_running():
            time.sleep(0.5)
            sys.stdout.write(f"\r{dl.status:14} {dl.progress*100:5.1f}%  {human_size(dl.downloaded)}/{human_size(dl.size)}"
                             f"  {human_size(dl.speed)}/s  sisa {human_time(dl.eta)}  koneksi {dl.active_conns}   ")
            sys.stdout.flush()
    except KeyboardInterrupt:
        dl.pause()
        dl.wait()
    print(f"\n{dl.status}: {dl.path}" + (f" ({dl.error})" if dl.error else ""))
    return 0 if dl.status == Status.COMPLETED else 1


if __name__ == "__main__":
    sys.exit(main())
