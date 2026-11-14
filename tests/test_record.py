"""Session recordings: made while a session runs, saved with their start time, served with seeking,
frames cut per console line, pinned, and kept within recording_max_gb. Needs ffmpeg (skipped without)."""
import json
import os
import shutil
import time
import unittest
import urllib.request

from rig import srv, wait_for
from test_http import HttpCase

FFMPEG = shutil.which("ffmpeg")


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class RecordingTests(HttpCase):
    config = {
        "api_token": "secret",
        "capture_input": ["-re", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30"],
        "capture_audio_input": ["-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"],
        "capture_idle_seconds": 2,
        "recording_preset": "ultrafast",
    }

    def setUp(self):
        super().setUp()
        self.mgr.recorder.base = self.base

    def tearDown(self):
        a = self.mgr.recorder.active
        if a:
            a["proc"].kill()
            a["proc"].wait()
        super().tearDown()

    def record_session(self, owner="alice", seconds=3.0, lines=("boot",)):
        token = self.grant(owner, "game")
        uid = self.mgr.lease["id"]
        self.assertEqual(self.mgr.status()["recording"]["active"], uid)
        time.sleep(seconds / 2)
        stamped = time.time()
        self.mgr.console_append(token, [[stamped, text] for text in lines], sent=stamped)
        time.sleep(seconds / 2)
        self.mgr.release(token)
        self.assertTrue(wait_for(lambda: (self.mgr.store.session(uid) or {}).get("recording"), timeout=40),
                        f"recording not saved: {self.mgr.recorder.error}")
        return uid, stamped

    def test_session_is_recorded_with_its_start_time(self):
        uid, _ = self.record_session(seconds=3)
        s = self.mgr.store.session(uid)
        rec = s["recording"]
        self.assertTrue(os.path.exists(self.mgr.recorder.path(uid)))
        self.assertGreater(rec["duration"], 1.0)
        self.assertLess(rec["duration"], 6.0)
        # the first frame arrived shortly after the lease was granted, on the same clock
        self.assertGreaterEqual(rec["t0"], s["start"] - 0.5)
        self.assertLess(rec["t0"], s["start"] + 3.0)
        self.assertTrue(rec["audio"])
        # both streams made it into the file, and the sound covers the picture (it was lined up, not dropped)
        import subprocess
        info = subprocess.run([FFMPEG, "-hide_banner", "-i", self.mgr.recorder.path(uid)],
                              capture_output=True, text=True).stderr
        self.assertIn("Video: h264", info)
        # every frame of the feed (30 fps) is kept with its own millisecond time; reading the MJPEG stream over
        # HTTP used to round them onto a 1/25 s grid (40 ms steps)
        from dcmgr.calibrate import frame_lumas
        pts = [p for p, _ in frame_lumas(self.mgr.recorder.path(uid), FFMPEG)]
        step = sorted(b - a for a, b in zip(pts, pts[1:]))[len(pts) // 2]
        self.assertTrue(0.025 < step < 0.037, step)  # ~33 ms: 30 fps, each frame with its own time
        self.assertIn("Audio: aac", info)
        self.assertIsNone(self.mgr.status()["recording"]["active"])
        self.assertEqual(os.listdir(self.mgr.recorder.dir), [f"{uid}.mp4"])  # no part file left

    def test_video_is_served_with_ranges(self):
        uid, _ = self.record_session(seconds=2)
        size = self.mgr.store.session(uid)["recording"]["bytes"]
        code, ctype, data = self.request(f"/recordings/{uid}.mp4", token=None)
        self.assertEqual((code, ctype, len(data)), (200, "video/mp4", size))
        req = urllib.request.Request(f"{self.base}/recordings/{uid}.mp4", headers={"Range": "bytes=10-109"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 206)
            self.assertEqual(r.headers["Content-Range"], f"bytes 10-109/{size}")
            self.assertEqual(r.read(), data[10:110])
        req = urllib.request.Request(f"{self.base}/recordings/{uid}.mp4", headers={"Range": "bytes=-50"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.read(), data[-50:])
        self.assertEqual(self.request("/recordings/000000000000.mp4", token=None)[0], 404)

    def test_frame_per_console_line(self):
        uid, stamped = self.record_session(seconds=3, lines=("hello",))
        line = self.mgr.console_read(session=uid)["lines"][0]
        code, ctype, jpg = self.request(f"/sessions/{uid}/frame.jpg?t={line['t']}", token=None)
        self.assertEqual((code, ctype), (200, "image/jpeg"))
        self.assertEqual(jpg[:2], b"\xff\xd8")
        # times outside the recording are clamped to its first/last frame rather than failing
        self.assertEqual(self.request(f"/sessions/{uid}/frame.jpg?t=0", token=None)[0], 200)
        self.assertEqual(self.request(f"/sessions/{uid}/frame.jpg", token=None)[0], 400)

    def test_frame_of_a_live_session_is_refused(self):
        self.grant()
        uid = self.mgr.lease["id"]
        self.assertEqual(self.request(f"/sessions/{uid}/frame.jpg?t={time.time()}", token=None)[0], 409)

    def test_budget_deletes_oldest_but_keeps_pinned(self):
        first, _ = self.record_session("alice", seconds=2)
        second, _ = self.record_session("bob", seconds=2)
        code, r = self.post("/sessions/pin", {"uid": first, "owner": "admin"})
        self.assertEqual((code, r["pinned"]), (200, True))
        self.cfg["recording_max_gb"] = 1e-9  # anything is over budget now
        third, _ = self.record_session("carol", seconds=2)
        self.assertTrue(os.path.exists(self.mgr.recorder.path(first)))   # pinned
        self.assertFalse(os.path.exists(self.mgr.recorder.path(second)))  # oldest unpinned
        self.assertIsNone(self.mgr.store.session(second)["recording"])
        self.assertTrue(self.mgr.store.session(first)["pinned"])
        self.assertIn("session.pinned", self.kinds())
        self.assertEqual(self.post("/sessions/pin", {"uid": "000000000000"})[0], 404)

    def test_disabled_or_low_disk(self):
        self.cfg["recording_min_free_mb"] = 10 ** 12
        self.grant()
        self.assertIsNone(self.mgr.recorder.active)
        self.assertIn("recording.skipped", self.kinds())


class NoCaptureTests(HttpCase):
    def test_rigs_without_capture_never_record(self):
        self.assertFalse(self.mgr.status()["recording"]["enabled"])
        self.mgr.release(self.grant())
        uid = self.mgr.history[-1]["uid"]
        self.assertIsNone(self.mgr.store.session(uid)["recording"])
        self.assertEqual(self.request(f"/sessions/{uid}/frame.jpg?t=1", token=None)[0], 404)


if __name__ == "__main__":
    unittest.main()
