"""Shared ffmpeg feeds from the capture device: picture (MJPEG) and sound (MP3)."""
import re
import subprocess
import threading
import time
from collections import deque
from .common import ApiError, log

class Feed:
    """One ffmpeg process whose output every viewer/listener shares, so the capture device is only
    opened once. It runs only while someone wants it, and stops capture_idle_seconds after the
    last one leaves."""

    what = "feed"

    def __init__(self, cfg, enabled, backlog=1):
        self.cfg, self.enabled = cfg, enabled
        self.cond = threading.Condition()
        self.items = deque(maxlen=backlog)        # (seq, time, data)
        self.seq = 0
        self.proc, self.error, self._retry_at = None, None, 0.0
        self.users, self.last_want = 0, 0.0
        self.rate, self._rate_n, self._rate_t = 0.0, 0, 0.0
        if enabled:
            threading.Thread(target=self._reaper, daemon=True).start()

    def command(self):
        raise NotImplementedError

    def split(self, buf):
        """Cut complete items off the front of buf; returns (items, rest)."""
        return [buf], b""

    def status(self):
        with self.cond:
            running = bool(self.proc and self.proc.poll() is None)
            return {"enabled": self.enabled, "running": running, "users": self.users,
                    "rate": round(self.rate, 1) if running else 0, "error": self.error}

    def _want(self):
        """Note that someone wants output and start ffmpeg if needed (caller holds self.cond)."""
        self.last_want = time.time()
        if (self.proc and self.proc.poll() is None or time.time() < self._retry_at
                or getattr(self, "_restarting", False)):
            return
        cmd = self.command()
        log.info("%s: starting %s", self.what, " ".join(cmd))
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE)
        except OSError as e:
            self.proc, self.error, self._retry_at = None, f"cannot run ffmpeg: {e}", time.time() + 10
            return
        self._rate_n, self._rate_t = 0, time.time()
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        errs = deque(maxlen=5)

        def read_errors():
            with proc.stderr:
                for line in proc.stderr:
                    errs.append(line.decode(errors="replace").strip())

        errors = threading.Thread(target=read_errors, daemon=True)
        errors.start()
        buf = b""
        with proc.stdout:  # closed when ffmpeg exits, including when it's stopped or restarted
            while True:
                chunk = proc.stdout.read1(1 << 16)
                if not chunk:
                    break
                items, buf = self.split(buf + chunk)
                for item in items:
                    self._publish(item)
        rc = proc.wait()
        errors.join(2)
        with self.cond:
            if self.proc is proc:  # not stopped by the reaper
                self.proc = None
                self.error = errs[-1] if errs else f"ffmpeg exited with code {rc}"
                self._retry_at = time.time() + 5
                log.warning("%s: %s", self.what, self.error)
            self.cond.notify_all()

    def _publish(self, data):
        with self.cond:
            now = time.time()
            self.seq += 1
            self.items.append((self.seq, now, data))
            self.error = None
            self._rate_n += 1
            if now - self._rate_t >= 2:
                self.rate, self._rate_n, self._rate_t = self._rate_n / (now - self._rate_t), 0, now
            self.cond.notify_all()

    def _reaper(self):
        while True:
            time.sleep(2)
            with self.cond:
                p = self.proc
                if (p and p.poll() is None and not self.users
                        and time.time() - self.last_want > self.cfg["capture_idle_seconds"]):
                    log.info("%s: nobody watching, stopping ffmpeg", self.what)
                    self.proc = None
                    self.items.clear()
                    p.terminate()

    def _check_enabled(self):
        if not self.enabled:
            raise ApiError(404, f"no {self.what} device configured")

    def follow(self, every=False):
        """Yield lists of new items as they arrive: all of them in order (audio), or only the
        latest (video, so a slow viewer skips frames instead of lagging). Ends if the device fails."""
        self._check_enabled()
        with self.cond:
            self.users += 1
            last = self.seq if every else 0
        try:
            while True:
                with self.cond:
                    deadline = time.time() + 10
                    while not self.items or self.items[-1][0] <= last:
                        self._want()
                        if time.time() > deadline or (self.error and not self.proc):
                            return
                        self.cond.wait(1.0)
                    # a listener that fell behind the backlog just skips ahead
                    out = [d for s, _, d in self.items if s > last] if every else [self.items[-1][2]]
                    last = self.items[-1][0]
                yield out
        finally:
            with self.cond:
                self.users -= 1
                self.last_want = time.time()


def crop_from_filter(vf, size):
    """The crop of a pixel-based capture_filter ("crop=W:H:X:Y,...") as fractions of the capture_size frame,
    so the interactive adjuster can start from it. None if it can't be worked out."""
    m = re.match(r"\s*crop=(\d+):(\d+)(?::(\d+):(\d+))?", vf or "")
    s = re.fullmatch(r"(\d+)x(\d+)", size or "")
    if not m or not s:
        return None
    cw, ch = int(m.group(1)), int(m.group(2))
    sw, sh = int(s.group(1)), int(s.group(2))
    # ffmpeg centres the crop when x/y are left out
    x = int(m.group(3)) if m.group(3) is not None else (sw - cw) // 2
    y = int(m.group(4)) if m.group(4) is not None else (sh - ch) // 2
    if not (0 < cw <= sw and 0 < ch <= sh):
        return None
    return {"x": round(x / sw, 5), "y": round(y / sh, 5), "w": round(cw / sw, 5), "h": round(ch / sh, 5)}


class VideoFeed(Feed):
    """The DC's screen from a capture device, so screenshots never go through the loader or the network adapter.

    The picture can be cropped interactively from the web GUI (to centre an off-centre image): `crop` holds
    the saved crop as fractions of the source frame, and while `adjusting` the feed shows the whole uncropped
    frame (scaled to the same output size, so recordings and viewers carry on undisturbed)."""

    what = "capture"

    def __init__(self, cfg):
        super().__init__(cfg, bool(cfg["capture_device"] or cfg["capture_input"]))
        self.crop = None          # {"x", "y", "w", "h"} fractions of the source frame, or None: use capture_filter
        self.adjust_until = 0.0   # showing the whole frame for the adjuster until this time

    @property
    def adjusting(self):
        return time.time() < self.adjust_until

    def output_size(self):
        w, _, h = self.cfg["capture_output_size"].partition("x")
        return int(w), int(h)

    def filter(self):
        """The ffmpeg -vf in effect: whole frame while adjusting, else the saved crop, else capture_filter."""
        w, h = self.output_size()
        if self.adjusting:
            return f"scale={w}:{h}"
        if self.crop:
            c = self.crop
            return (f"crop=w=iw*{c['w']:.5f}:h=ih*{c['h']:.5f}:x=iw*{c['x']:.5f}:y=ih*{c['y']:.5f},"
                    f"scale={w}:{h}")
        return self.cfg["capture_filter"]

    def restart(self):
        """Pick up a new filter: stop ffmpeg. Viewers, screenshots and recordings keep their connections and
        get frames from the new one, which starts as soon as anyone wants a frame. The old one has to have
        exited first: a V4L2 device can only be open once ("Resource busy")."""
        with self.cond:
            p, self.proc = self.proc, None
            self._restarting = True
        try:
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
        finally:
            with self.cond:
                self._restarting = False
                self._retry_at = 0.0
                self.error = None
                self.cond.notify_all()

    def status(self):
        st = super().status()
        st.update(adjusting=self.adjusting, cropped=bool(self.crop))
        return st

    def command(self):
        c = self.cfg
        inp = c["capture_input"] or (["-f", "v4l2", "-input_format", "mjpeg"]
                                     + (["-video_size", c["capture_size"]] if c["capture_size"] else [])
                                     + ["-i", c["capture_device"]])
        vf = self.filter()
        vf = ["-vf", vf] if vf else []
        return [c["capture_ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostdin", *inp, "-an", *vf,
                "-r", str(c["capture_fps"]), "-q:v", str(c["capture_quality"]), "-f", "mjpeg", "pipe:1"]

    def split(self, buf):
        # ffmpeg's mjpeg muxer writes back-to-back JPEGs; 0xFFD9 (EOI) can't occur inside one
        # because the encoder byte-stuffs 0xFF in entropy-coded data
        out = []
        while True:
            s = buf.find(b"\xff\xd8")
            e = buf.find(b"\xff\xd9", s + 2) if s >= 0 else -1
            if e < 0:
                return out, (buf[s:] if s >= 0 else b"")
            out.append(buf[s:e + 2])
            buf = buf[e + 2:]

    def snapshot(self, timeout=10.0):
        """A JPEG captured after this call started, so it shows the screen as it is now."""
        self._check_enabled()
        t0 = time.time()
        with self.cond:
            while not self.items or self.items[-1][1] <= t0:
                self._want()
                left = t0 + timeout - time.time()
                if left <= 0 or (self.error and not self.proc):
                    raise ApiError(503, f"no picture from the capture device: {self.error or 'timed out'}")
                self.cond.wait(min(left, 1.0))
            return self.items[-1][2]


class AudioFeed(Feed):
    """The DC's sound from the capture stick's USB audio, as an endless MP3 stream."""

    what = "audio"

    def __init__(self, cfg):
        super().__init__(cfg, bool(cfg["capture_audio_device"] or cfg["capture_audio_input"]), backlog=256)

    def command(self):
        c = self.cfg
        inp = c["capture_audio_input"] or ["-f", "alsa", "-i", c["capture_audio_device"]]
        return [c["capture_ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostdin", *inp, "-vn",
                "-ar", "48000", "-ac", "2", "-c:a", "libmp3lame", "-b:a", c["capture_audio_bitrate"],
                "-flush_packets", "1", "-f", "mp3", "pipe:1"]
