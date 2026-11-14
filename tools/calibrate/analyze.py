#!/usr/bin/env python3
"""Measure capture_delay_ms from a calibration session.

Run tools/calibrate/calib.elf on the DC through dcm (it flashes the screen white and prints
"CAL flash <n> fill_us=<us>" as it does), then run this. It reads that session's console output and
recording from dcmgr, finds where each flash appears in the recording, and compares that with when
the flash happened according to the console. The median difference is how far the captured picture
lags the DC: set capture_delay_ms to it.

  python3 analyze.py --url http://dcmgr-host            # the latest session of project "calibration"
  python3 analyze.py --url http://dcmgr-host --session ID [--json]

Needs Python 3.8+ and ffmpeg on PATH (to decode the recording).
"""
import argparse
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dcmgr.calibrate import analyze  # noqa: E402  (the same analysis the web GUI's Calibrate uses)


def get(url, raw=False):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read() if raw else json.load(r)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=os.environ.get("DCMGR_URL", "http://localhost:8731"))
    ap.add_argument("--session", help="session id (default: the latest session of project --project)")
    ap.add_argument("--project", default="calibration")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    base = args.url.rstrip("/")

    q = {"uid": args.session} if args.session else {"project": args.project}
    sessions = get(f"{base}/sessions?{urllib.parse.urlencode(dict(q, limit=1))}")["sessions"]
    if not sessions:
        raise SystemExit(f"no finished session found ({q}); run calib.elf with: dcm load calib.elf -p calibration")
    s = sessions[-1]
    rec = s.get("recording")
    if not rec:
        raise SystemExit(f"session {s['uid']} has no recording (no capture device, or it isn't saved yet)")
    lines = get(f"{base}/console?session={s['uid']}&limit=10000")["lines"]
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "rec.mp4")
        with open(path, "wb") as f:
            f.write(get(f"{base}/recordings/{s['uid']}.mp4", raw=True))
        try:
            result = analyze(lines, path, rec["t0"], args.ffmpeg)
        except ValueError as e:
            raise SystemExit(f"session {s['uid']}: {e}")
    result["session"] = s["uid"]
    recommended, window, step = result["recommended_ms"], result["exact_window_ms"], result["frame_ms"]
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"session {s['uid']}: {result['matched']} of {result['flashes']} flashes found in the recording "
          f"(one frame every {step:.0f} ms)")
    for i, d in enumerate(result["delays_ms"], 1):
        print(f"  flash {i:2d}: picture {d:6.0f} ms after the DC")
    print(f"median {result['median_ms']} ms, range {result['min_ms']}-{result['max_ms']} ms (the spread is the "
          f"capture's {step:.0f} ms frame interval: each flash appears on the next frame captured)")
    if window:
        print(f"any capture_delay_ms in ({window[0]:.0f}, {window[1]:.0f}] puts every flash line on its first white "
              f"frame; recommended: {recommended} ms")
    else:
        print(f"the capture's timing jitter is wider than a frame, so no one value is exact for every line; "
              f"recommended: {recommended} ms, the smallest at which every line's frame already shows its change "
              f"(some lines then show it one frame later than its first)")
    print(f'set in dcmgr.json:  "capture_delay_ms": {recommended}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
