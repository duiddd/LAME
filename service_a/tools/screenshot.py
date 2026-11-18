"""Saves a page as a PNG with a headless Edge or Chrome: evidence shots of a dashboard.

  python service_a/tools/screenshot.py --url URL --out FILE.png [--from 2026-11-18T14:23:00 --to 2026-11-18T14:30:00]

--from/--to (local time) are appended as Grafana's from= and to= in epoch milliseconds, so a
Grafana dashboard URL shows exactly the window of a fault run. The page must open without a login.
"""
import argparse
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

BROWSERS = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            "/usr/bin/google-chrome", "/usr/bin/chromium"]


def browser():
    for b in BROWSERS:
        if Path(b).exists():
            return b
    raise SystemExit("No Edge or Chrome found; take the screenshot by hand")


def capture(url, path: Path, width, height):
    # A fresh profile per shot, and one retry: headless Edge sometimes crashes on start.
    # 每張圖用新的 profile,並重試一次:無頭 Edge 偶爾在啟動時崩潰。
    exe = browser()
    for _ in (1, 2):
        path.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory() as profile:
            done = subprocess.run([exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={profile}",
                                   f"--window-size={width},{height}", "--virtual-time-budget=20000",
                                   f"--screenshot={path.resolve()}", url], capture_output=True, timeout=120)
        if done.returncode == 0 and path.exists():
            print("saved", path.resolve())
            return
    raise SystemExit(f"Could not capture {url}: exit {done.returncode}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--from", dest="start")
    parser.add_argument("--to", dest="end")
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=2300)
    args = parser.parse_args()
    url = args.url
    if args.start and args.end:
        ms = [int(datetime.fromisoformat(t).timestamp() * 1000) for t in (args.start, args.end)]
        url += ("&" if "?" in url else "?") + f"from={ms[0]}&to={ms[1]}"
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    capture(url, out, args.width, args.height)


if __name__ == "__main__":
    main()
