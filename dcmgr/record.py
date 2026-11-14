"""Session recordings: the capture device's picture and sound, one video per session, in a size-capped ring.

While a session runs, the server pipes every frame of its shared capture feed (the capture device stays
shared with live viewers) into ffmpeg, which stamps each with the wall-clock time it arrived, to the
millisecond, and encodes it to a Matroska file (-copyts). (Reading the server's MJPEG stream over HTTP instead
rounds the stamps to a 1/25 s grid: that demuxer's time base is fixed.) The server also writes the MP3 sound
stream to a file and notes when its first chunk arrived. (Wall-
clock stamps on the MP3 input aren't reliable across ffmpeg versions, and one input stamped near 0 next to
one stamped 1.8e9 stalls the muxer, so the sound is lined up by hand instead.) When the session ends both
are muxed into an MP4 that starts at 0 with the sound shifted to match, and the wall-clock time of the first
frame is stored as t0. A console line printed at time T is then on screen at video position
T - t0 + capture_delay_ms.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time

from .common import ApiError, log

PART = ".part.mkv"
AUDIO_PART = ".part.mp3"


class Recorder:
    def __init__(self, cfg, store, event, video_feed, audio_feed, state_dir):
        self.cfg, self.store, self.event = cfg, store, event
        self.video_feed, self.audio_feed = video_feed, audio_feed
        self.audio = bool(audio_feed and audio_feed.enabled)
        self.enabled = bool(cfg["record"] and video_feed and video_feed.enabled)
        self.dir = cfg["recordings_dir"] or os.path.join(state_dir, "recordings")
        self.base = None          # this server's URL (set at startup; not needed for recording any more)
        self.delay_ms = cfg["capture_delay_ms"]  # in use; the manager sets it when Calibrate applies one
        self.lock = threading.Lock()
        self.active = None        # {"uid", "proc", "part", "since"}
        self.error = None
        if self.enabled:
            try:
                os.makedirs(self.dir, exist_ok=True)
            except OSError as e:
                self.enabled, self.error = False, f"cannot create {self.dir}: {e}"
                log.warning("recording disabled: %s", self.error)
        if self.enabled:  # a crash or restart left half-written recordings: finish them
            for part in glob.glob(os.path.join(self.dir, "*" + PART)):
                uid = os.path.basename(part)[:-len(PART)]
                # the sound's start time died with the old process: keep the picture only
                a = {"uid": uid, "part": part, "proc": None, "mp3": part[:-len(PART)] + AUDIO_PART,
                     "audio_t0": None, "stop": threading.Event(), "thread": None, "video_thread": None}
                threading.Thread(target=self._finalize, args=(a,), daemon=True).start()

    def status(self):
        a = self.active
        return {"enabled": self.enabled, "active": a["uid"] if a else None, "since": a["since"] if a else None,
                "error": self.error, "delay_ms": self.delay_ms}

    def path(self, uid):
        return os.path.join(self.dir, f"{uid}.mp4")

    # recording

    def start(self, uid):
        """Begin recording session `uid` (called when a lease or hold is granted)."""
        if not self.enabled:
            return
        with self.lock:
            if self.active:  # shouldn't happen (one session at a time), but never leave one running
                self._stop_locked()
            try:
                free = shutil.disk_usage(self.dir).free
            except OSError:
                free = 0
            if free < self.cfg["recording_min_free_mb"] * 1024 * 1024:
                self.error = f"not recording: only {free // 2 ** 20} MB free in {self.dir}"
                self.event("recording.skipped", self.error, 30, session=uid)
                return
            part = os.path.join(self.dir, uid + PART)
            # frames arrive on stdin as JPEGs; -framerate 1000 gives the raw MJPEG demuxer a 1 ms time base, so
            # the wall-clock stamps keep millisecond precision, and passthrough keeps every frame as it came
            cmd = [self.cfg["capture_ffmpeg"], "-hide_banner", "-loglevel", "error",
                   "-use_wallclock_as_timestamps", "1", "-f", "mjpeg", "-framerate", "1000", "-i", "pipe:0",
                   "-map", "0:v", "-c:v", "libx264", "-preset", self.cfg["recording_preset"],
                   "-crf", str(self.cfg["recording_crf"]), "-pix_fmt", "yuv420p", "-copyts",
                   "-fps_mode", "passthrough", "-enc_time_base", "1/1000",
                   "-f", "matroska", "-y", part]
            try:
                # closing stdin (end of input) makes ffmpeg finish the file properly
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE)
            except OSError as e:
                self.error = f"cannot run ffmpeg: {e}"
                log.warning("recording: %s", self.error)
                return
            self.error = None
            a = {"uid": uid, "proc": proc, "part": part, "since": time.time(), "stop": threading.Event(),
                 "mp3": os.path.join(self.dir, uid + AUDIO_PART), "audio_t0": None, "thread": None}
            a["video_thread"] = threading.Thread(target=self._feed_video, args=(a,), daemon=True)
            a["video_thread"].start()
            if self.audio:
                a["thread"] = threading.Thread(target=self._record_audio, args=(a,), daemon=True)
                a["thread"].start()
            self.active = a
            log.info("recording session %s", uid)

    def _feed_video(self, a):
        """Pipe every frame of the shared capture feed into the recording ffmpeg until the session ends."""
        proc = a["proc"]
        try:
            for batch in self.video_feed.follow(every=True):
                for jpg in batch:
                    proc.stdin.write(jpg)
                proc.stdin.flush()
                if a["stop"].is_set():
                    break
        except (OSError, ValueError, ApiError) as e:  # ffmpeg gone, or the capture device failed
            if not a["stop"].is_set():
                log.warning("recording %s: picture stopped: %s", a["uid"], e)
        finally:
            try:
                proc.stdin.close()  # end of input: ffmpeg finishes the file
            except OSError:
                pass

    def _record_audio(self, a):
        """Write the shared MP3 stream to a file, noting when the first chunk arrived."""
        try:
            with open(a["mp3"], "wb") as f:
                for batch in self.audio_feed.follow(every=True):
                    if a["audio_t0"] is None and batch:
                        a["audio_t0"] = time.time()
                    for chunk in batch:
                        f.write(chunk)
                    if a["stop"].is_set():
                        break
        except (OSError, ApiError) as e:
            log.warning("recording %s: sound not recorded: %s", a["uid"], e)

    def stop(self, uid):
        """Stop recording `uid` (its session row must exist) and finish the file in the background."""
        with self.lock:
            if self.active and self.active["uid"] == uid:
                self._stop_locked()

    def _stop_locked(self):
        a, self.active = self.active, None
        threading.Thread(target=self._finalize, args=(a,), daemon=True).start()

    def _finalize(self, a):
        uid, part, proc = a["uid"], a["part"], a["proc"]
        a["stop"].set()
        err = ""
        if proc is not None:
            if a.get("video_thread"):
                a["video_thread"].join(5)  # it closes ffmpeg's stdin once it sees the stop
            proc.stdin = None  # already closed by _feed_video; communicate() must not touch it
            try:
                err = proc.communicate(timeout=20)[1].decode(errors="replace").strip()
            except subprocess.TimeoutExpired:
                proc.kill()
                err = proc.communicate()[1].decode(errors="replace").strip()
        if a["thread"]:
            a["thread"].join(5)
        t0, _ = self._probe(part)  # with -copyts the part file's start is the first frame's wall-clock time
        out = self.path(uid)
        ok = t0 is not None
        duration = None
        mp3, audio = a["mp3"], False
        if ok:
            cmd = [self.cfg["capture_ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostdin", "-i", part]
            if a["audio_t0"] and os.path.exists(mp3) and os.path.getsize(mp3) > 1024:
                # each input is moved to start at 0, then the sound shifted to where it began relative to the
                # first frame (or its head cut off if it began earlier)
                offset = a["audio_t0"] - t0
                cmd += (["-itsoffset", f"{offset:.3f}"] if offset >= 0 else ["-ss", f"{-offset:.3f}"])
                cmd += ["-i", mp3, "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "96k"]
                audio = True
            else:
                cmd += ["-map", "0:v"]
            cmd += ["-c:v", "copy", "-movflags", "+faststart", "-y", out]
            r = subprocess.run(cmd, capture_output=True)
            ok = r.returncode == 0 and os.path.exists(out)
            err = err or r.stderr.decode(errors="replace").strip()
            duration = self._probe(out)[1] if ok else None  # the MP4 starts at 0, so this is its length
            ok = bool(ok and duration)
        for f in (part, mp3):
            try:
                os.remove(f)
            except OSError:
                pass
        if not ok:
            why = (err.splitlines() or ["no picture was recorded"])[-1]
            self.error = f"recording of session {uid} failed: {why}"
            self.event("recording.failed", self.error, 30, session=uid)
            return
        size = os.path.getsize(out)
        meta = {"file": os.path.basename(out), "t0": t0, "duration": round(duration, 3), "bytes": size,
                "audio": audio}
        try:
            self.store.update_session(uid, recording=json.dumps(meta))
        except Exception as e:  # noqa: BLE001 - never let a bookkeeping error lose the file silently
            log.warning("recording %s: could not store its details: %s", uid, e)
        log.info("recording of session %s saved: %.0f s, %.1f MB", uid, duration, size / 2 ** 20)
        self.enforce_budget(keep=uid)

    def _probe(self, path):
        """(wall-clock time of the first frame, duration in s) of a recording, from ffmpeg's own report."""
        try:
            r = subprocess.run([self.cfg["capture_ffmpeg"], "-hide_banner", "-nostdin", "-i", path],
                               capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None, None
        text = r.stderr.decode(errors="replace")
        start = re.search(r"start: (-?[\d.]+)", text)
        dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", text)
        if not start or not dur:
            return None, None
        h, m, sec = dur.groups()
        return float(start.group(1)), int(h) * 3600 + int(m) * 60 + float(sec)

    # retention

    def enforce_budget(self, keep=None):
        """Delete the oldest recordings (never pinned ones, nor `keep`, the one just saved: a single session
        longer than the budget is still kept until the next one) until they fit in recording_max_gb."""
        budget = self.cfg["recording_max_gb"] * 2 ** 30
        files = []
        for f in glob.glob(os.path.join(self.dir, "*.mp4")):
            try:
                st = os.stat(f)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, f))
        total = sum(size for _, size, _ in files)
        if total <= budget:
            return
        pinned = self.store.pinned_uids()
        for _, size, f in sorted(files):
            if total <= budget:
                break
            uid = os.path.basename(f)[:-4]
            if uid in pinned or uid == keep:
                continue
            try:
                os.remove(f)
            except OSError as e:
                log.warning("could not delete old recording %s: %s", f, e)
                continue
            total -= size
            self.store.update_session(uid, recording=None)
            log.info("deleted recording of session %s to stay within %s GB", uid, self.cfg["recording_max_gb"])
        if total > budget:
            log.warning("pinned recordings alone use %.1f GB, more than recording_max_gb", total / 2 ** 30)

    # reading

    def frame(self, uid, t):
        """JPEG of the recorded screen at wall-clock time t (e.g. when a console line was printed)."""
        meta = self._meta(uid)
        pos = t - meta["t0"] + self.delay_ms / 1000
        pos = max(0.0, min(pos, meta["duration"] - 0.05))
        try:
            r = subprocess.run([self.cfg["capture_ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostdin",
                                "-ss", f"{pos:.3f}", "-i", self.path(uid), "-frames:v", "1", "-q:v", "3",
                                "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ApiError(503, f"cannot extract the frame: {e}") from None
        if r.returncode or not r.stdout:
            raise ApiError(503, "cannot extract the frame: " + r.stderr.decode(errors="replace").strip()[-200:])
        return r.stdout

    def _meta(self, uid):
        s = self.store.session(uid)
        if s is None:
            a = self.active
            raise ApiError(409 if a and a["uid"] == uid else 404,
                           "the session is still being recorded; frames are available once it ends"
                           if a and a["uid"] == uid else "no such session")
        if not s.get("recording") or not os.path.exists(self.path(uid)):
            raise ApiError(404, "this session has no recording (not recorded, still being saved, or deleted "
                                "to stay within recording_max_gb)")
        return s["recording"]
