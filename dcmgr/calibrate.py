"""Capture-delay calibration: run tools/calibrate/calib.elf on the DC (it flashes the screen white and prints
"CAL flash <n> fill_us=<us>" as it does), find each flash in the session's recording, and work out the
capture_delay_ms that puts every console line on a frame that already shows its change.

The analysis is shared by the web GUI (through Calibrator) and tools/calibrate/analyze.py."""
import os
import re
import statistics
import subprocess
import threading
import time

from .common import ApiError, HERE, log

LINE_RE = re.compile(r"^CAL flash (\d+) fill_us=(\d+)")
PROJECT = "calibration"


# --- analysis -------------------------------------------------------------------------------------------

def frame_lumas(path, ffmpeg="ffmpeg"):
    """[(pts seconds, mean luma 0-255), ...] for every video frame of a recording."""
    r = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-i", path, "-an", "-vf", "showinfo", "-f", "null", "-"],
                       capture_output=True, text=True)
    frames = []
    for line in r.stderr.splitlines():
        if "showinfo" not in line or "pts_time:" not in line:
            continue
        pts = re.search(r"pts_time:\s*([\d.]+)", line)
        mean = re.search(r"mean:\[(\d+)", line)
        if pts and mean:
            frames.append((float(pts.group(1)), int(mean.group(1))))
    if not frames:
        raise ValueError(f"no frames decoded from the recording: {r.stderr.strip()[-300:]}")
    return frames


def onsets(frames):
    """Times of the frames where the picture goes from dark to bright (the first white frame of a flash),
    using a threshold halfway between the darkest and brightest frames."""
    lo, hi = min(m for _, m in frames), max(m for _, m in frames)
    if hi - lo < 40:
        raise ValueError(f"no flashes in the recording (brightness only ranges {lo}-{hi}): "
                         "is the capture showing the DC?")
    mid = (lo + hi) / 2
    out, prev_bright = [], frames[0][1] > mid
    for pts, mean in frames[1:]:
        bright = mean > mid
        if bright and not prev_bright:
            out.append(pts)
        prev_bright = bright
    return out


def flash_lines(lines):
    """[(flash number, line time, wall-clock time the screen went white), ...] from console lines. Each line
    is printed right after the fill, so the screen changed fill_us before it."""
    out = []
    for ln in lines:
        m = LINE_RE.match(ln["text"])
        if m:
            out.append((int(m.group(1)), ln["t"], ln["t"] - int(m.group(2)) / 1e6))
    return out


def pair(flashes, onset_times, t0, window=1.0):
    """How long after each flash it showed up in the recording: [(n, seconds), ...]."""
    out, used = [], set()
    for n, _, when in flashes:
        rel = when - t0
        cand = [v for v in onset_times if v not in used and rel - 0.1 <= v <= rel + window]
        if cand:
            used.add(cand[0])
            out.append((n, cand[0] - rel))
    return out


def recommend(line_rels, frame_pts, onset_times):
    """The smallest capture_delay_ms at which every flash line's frame already shows the flash.

    The player and frame.jpg show the first frame at or after (line time + delay). For one line that lands on
    the flash's first frame for any delay in (previous frame - line, first white frame - line]. When the
    capture's jitter is under a frame, those windows overlap and any value in the overlap is exact; when it
    isn't, no single value is, and the smallest that never shows the frame before the change is the best
    trade: some lines then show a frame one interval later than their first. Returns
    (recommended ms, (lo ms, hi ms) of the overlap or None)."""
    los, his = [], []
    for rel in line_rels:
        on = next((o for o in onset_times if o > rel - 0.1), None)
        if on is None:
            continue
        prev = max((f for f in frame_pts if f < on - 1e-6), default=on)
        los.append(prev - rel)
        his.append(on - rel)
    if not los:
        raise ValueError("no flash line could be matched to the recording")
    lo, hi = max(los) * 1000, min(his) * 1000
    rec = int(-(-(lo + 1) // 5) * 5)  # just past the latest "previous frame", rounded up to 5 ms
    # only an exact window if the recommendation actually lies in it (a window narrower than the rounding isn't)
    return rec, ((lo, hi) if rec <= hi else None)


def analyze(lines, recording_path, t0, ffmpeg="ffmpeg"):
    """Everything about one calibration session, as a dict (see the keys below)."""
    flashes = flash_lines(lines)
    if not flashes:
        raise ValueError("the session printed no 'CAL flash' lines: was it calib.elf?")
    frames = frame_lumas(recording_path, ffmpeg)
    found = onsets(frames)
    delays = pair(flashes, found, t0)
    if not delays:
        raise ValueError(f"none of the {len(flashes)} flashes matched a change in the recording ({len(found)} found)")
    ms = [d * 1000 for _, d in delays]
    rec, window = recommend([t - t0 for _, t, _ in flashes], [f for f, _ in frames], found)
    step = statistics.median(b - a for (a, _), (b, _) in zip(frames, frames[1:])) * 1000
    return {"flashes": len(flashes), "matched": len(delays), "frame_ms": round(step, 1),
            "delays_ms": [round(x) for x in ms], "median_ms": round(statistics.median(ms)),
            "min_ms": round(min(ms)), "max_ms": round(max(ms)), "recommended_ms": rec,
            "exact_window_ms": [round(window[0], 1), round(window[1], 1)] if window else None}


# --- the web GUI's calibration ----------------------------------------------------------------------------

def elf_path(cfg):
    return cfg["calibration_elf"] or os.path.join(HERE, "tools", "calibrate", "calib.elf")


class Calibrator:
    """Runs a calibration on the DC from the server (when loader_command is set), analyses calibration
    sessions, and applies the result. One job at a time; the GUI polls status()."""

    def __init__(self, mgr):
        self.mgr = mgr
        self.lock = threading.Lock()
        self.job = None   # {"state", "by", "started", "session", "result", "error", "log"}
        self.last = None  # the latest analysis result, with its session id

    def status(self):
        cfg, mgr = self.mgr.cfg, self.mgr
        return {"delay_ms": mgr.recorder.delay_ms, "config_ms": mgr.config_delay_ms,
                "source": "calibrated" if mgr.calibrated_delay is not None else "config",
                "can_run": bool(cfg["loader_command"]) and os.path.exists(elf_path(cfg))
                and mgr.recorder.enabled,
                "loader": bool(cfg["loader_command"]), "recording": mgr.recorder.enabled,
                "job": dict(self.job) if self.job else None, "last": self.last}

    # analysis of a finished session

    def analyze_session(self, uid=None):
        st = self.mgr.store
        s = st.session(uid) if uid else st.latest_session(project=PROJECT)
        if s is None:
            raise ApiError(404, "no finished calibration session yet: run calib.elf as project "
                                f"'{PROJECT}' first" if not uid else "no such session")
        rec = s.get("recording")
        if not rec or not os.path.exists(self.mgr.recorder.path(s["uid"])):
            raise ApiError(409, f"session {s['uid']} has no recording (yet): it's needed to measure the delay")
        lines = st.console(s["uid"], 0, 10000)
        try:
            result = analyze(lines, self.mgr.recorder.path(s["uid"]), rec["t0"], self.mgr.cfg["capture_ffmpeg"])
        except ValueError as e:
            raise ApiError(422, f"session {s['uid']}: {e}") from None
        result.update(session=s["uid"], analyzed=time.time())
        self.last = result
        return result

    # running calib.elf from the server

    def start(self, by):
        cfg = self.mgr.cfg
        if not cfg["loader_command"]:
            raise ApiError(501, "no loader_command configured on the server: run calib.elf from a dev machine "
                                f"(dcm load calib.elf -p {PROJECT}) and analyse it here")
        if not os.path.exists(elf_path(cfg)):
            raise ApiError(501, f"calibration program not found at {elf_path(cfg)}")
        if not self.mgr.recorder.enabled:
            raise ApiError(501, "calibration needs session recordings (a capture device and \"record\": true)")
        with self.lock:
            if self.job and self.job["state"] in ("queued", "running", "analyzing"):
                raise ApiError(409, "a calibration is already running")
            self.job = {"state": "queued", "by": by, "started": time.time(), "session": None, "result": None,
                        "error": None, "log": []}
        threading.Thread(target=self._run, args=(by,), daemon=True).start()
        return self.status()

    def _note(self, msg):
        self.job["log"] = (self.job["log"] + [msg])[-12:]
        log.info("calibration: %s", msg)

    def _run(self, by):
        mgr, cfg, job = self.mgr, self.mgr.cfg, self.job
        owner, token, stop = by, None, threading.Event()  # the project says it's a calibration
        try:
            ticket = None
            while True:  # queue like any client
                r = mgr.acquire(owner, PROJECT, ticket, wait=20)
                if r["granted"]:
                    break
                ticket = r["ticket"]
            token = r["token"]
            job["session"] = mgr.lease["id"]

            def heartbeat():
                while not stop.wait(cfg["lease_ttl"] / 3):
                    try:
                        mgr.heartbeat(token)
                    except ApiError:
                        return
            threading.Thread(target=heartbeat, daemon=True).start()
            ip = mgr.prepare(token)["ip"]
            job["state"] = "running"
            cmd = [a.replace("{ip}", ip).replace("{file}", elf_path(cfg)) for a in cfg["loader_command"]]
            self._note("running " + " ".join(cmd))
            self._run_loader(cmd, token)
        except ApiError as e:
            job.update(state="failed", error=str(e))
        except Exception as e:  # noqa: BLE001 - report anything to the GUI rather than dying silently
            log.exception("calibration failed")
            job.update(state="failed", error=str(e))
        finally:
            stop.set()
            if token:
                try:
                    mgr.release(token)
                except ApiError:
                    pass
        if job["state"] == "failed":
            return
        job["state"] = "analyzing"
        self._note("waiting for the recording to be saved")
        for _ in range(120):
            s = mgr.store.session(job["session"])
            if s and s.get("recording"):
                break
            time.sleep(0.5)
        try:
            job["result"] = self.analyze_session(job["session"])
            job["state"] = "done"
            mgr.event("capture.calibration", f"calibration by {by}: recommended capture_delay_ms "
                      f"{job['result']['recommended_ms']}", by=by, session=job["session"],
                      recommended=job["result"]["recommended_ms"])
        except ApiError as e:
            job.update(state="failed", error=str(e))

    def _run_loader(self, cmd, token):
        """Run the loader, sending what it prints to the session's console like dcm does."""
        timeout = self.mgr.cfg["calibration_timeout"]
        env = dict(os.environ)
        if not os.access(env.get("HOME") or "/nonexistent", os.W_OK):
            # loaders keep settings under $HOME (kos-tool: ~/.config/kos-tool.cfg); services often lack one
            env["HOME"] = os.path.dirname(os.path.abspath(self.mgr.cfg["state_file"]))
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=env)
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        batch, sent_at = [], time.time()
        try:
            with proc.stdout:
                for raw in proc.stdout:
                    batch.append([time.time(), raw.decode("utf-8", "replace").rstrip("\r\n")])
                    if len(batch) >= 50 or time.time() - sent_at > 0.3:
                        self.mgr.console_append(token, batch, sent=time.time())
                        batch, sent_at = [], time.time()
            if batch:
                self.mgr.console_append(token, batch, sent=time.time())
            rc = proc.wait()
        finally:
            timer.cancel()
        if rc != 0:
            raise ApiError(502, f"the loader exited with {rc}" + (" (killed: timed out?)" if rc < 0 else "")
                           + "; see the session's console output")
        self._note("calib.elf finished")

