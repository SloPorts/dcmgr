"""Calibrate in the web GUI: applying a delay live, analysing sessions, and the server-run calibration job."""
import json
import os
import shutil
import sys
import textwrap
import time
import unittest

from rig import RigCase, srv, wait_for
from test_http import HttpCase

ApiError = srv.ApiError
FFMPEG = shutil.which("ffmpeg")
TESTSRC = ["-re", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15"]

FAKE_LOADER = textwrap.dedent('''
    import sys, time
    ip, elf = sys.argv[1], sys.argv[2]
    print("Connecting to", ip, flush=True)
    print("Upload <%s>" % elf, flush=True)
    for i in range(1, 4):
        time.sleep(0.2)
        print("CAL flash %d fill_us=8300" % i, flush=True)
    print("CAL done", flush=True)
    sys.exit(int(sys.argv[3]) if len(sys.argv) > 3 else 0)
''')


class DelayTests(RigCase):
    def test_apply_and_reset_persist(self):
        cal = self.mgr.calibrator
        self.assertEqual((cal.status()["delay_ms"], cal.status()["source"]), (100, "config"))
        st = self.mgr.set_capture_delay(145, "alice")
        self.assertEqual((st["delay_ms"], st["source"], st["config_ms"]), (145, "calibrated", 100))
        self.assertEqual(self.mgr.status()["recording"]["delay_ms"], 145)
        m2 = self.new_manager()
        self.assertEqual(m2.recorder.delay_ms, 145)
        self.assertEqual(self.cfg["capture_delay_ms"], 100)  # the loaded config is never changed
        st = m2.set_capture_delay(None, "alice", reset=True)
        self.assertEqual((st["delay_ms"], st["source"]), (100, "config"))
        self.assertIsNone(self.new_manager().calibrated_delay)
        self.assertIn("capture.delay", [e["kind"] for e in m2.events])

    def test_bad_values(self):
        for bad in ("soon", -5, 5000, None):
            with self.assertRaises(ApiError):
                self.mgr.set_capture_delay(bad, "alice")

    def test_analyze_needs_a_recorded_session(self):
        with self.assertRaises(ApiError) as cm:
            self.mgr.calibrator.analyze_session()
        self.assertEqual(cm.exception.code, 404)
        self.mgr.release(self.grant("bob", "calibration"))  # no capture device: not recorded
        with self.assertRaises(ApiError) as cm:
            self.mgr.calibrator.analyze_session()
        self.assertEqual(cm.exception.code, 409)

    def test_run_needs_a_loader_and_recordings(self):
        with self.assertRaises(ApiError) as cm:
            self.mgr.calibrator.start("alice")
        self.assertIn("loader_command", str(cm.exception))
        self.cfg["loader_command"] = ["true"]
        with self.assertRaises(ApiError) as cm:
            self.mgr.calibrator.start("alice")
        self.assertIn("recordings", str(cm.exception))
        self.assertFalse(self.mgr.calibrator.status()["can_run"])


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class RunTests(HttpCase):
    config = {"api_token": "secret", "capture_input": TESTSRC, "capture_idle_seconds": 2,
              "recording_preset": "ultrafast", "calibration_timeout": 30}

    def setUp(self):
        super().setUp()
        self.mgr.recorder.base = self.base
        loader = os.path.join(self.tmp, "loader.py")
        with open(loader, "w") as f:
            f.write(FAKE_LOADER)
        self.cfg["loader_command"] = [sys.executable, loader, "{ip}", "{file}"]
        self.analyzed = []
        # the video analysis is covered elsewhere (and on the real rig); here: the job around it
        self.mgr.calibrator.analyze_session = lambda uid=None: self.analyzed.append(uid) or {
            "recommended_ms": 95, "session": uid}

    def tearDown(self):
        a = self.mgr.recorder.active
        if a:
            a["proc"].kill()
            a["proc"].wait()
        super().tearDown()

    def wait_job(self):
        self.assertTrue(wait_for(lambda: self.mgr.calibrator.job["state"] in ("done", "failed"), timeout=60),
                        self.mgr.calibrator.job)
        return self.mgr.calibrator.job

    def test_one_button_calibration(self):
        code, st = self.post("/calibrate/run", {"owner": "alice (web)"})
        self.assertEqual(code, 200, st)
        self.assertTrue(st["can_run"])
        job = self.wait_job()
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(job["result"]["recommended_ms"], 95)
        uid = job["session"]
        self.assertEqual(self.analyzed, [uid])
        s = self.mgr.store.session(uid)
        self.assertEqual((s["owner"], s["project"]), ("alice (web)", "calibration"))
        texts = [l["text"] for l in self.mgr.console_read(session=uid)["lines"]]
        self.assertIn("CAL flash 3 fill_us=8300", texts)
        self.assertTrue(any(t.startswith("Upload <") and t.endswith("calib.elf>") for t in texts))
        self.assertIsNone(self.mgr.lease)  # released
        self.assertIn("capture.calibration", self.kinds())
        code, _, data = self.request("/calibrate", token=None)
        self.assertEqual(json.loads(data)["job"]["state"], "done")

    def test_loader_failure_is_reported_and_the_lock_released(self):
        self.cfg["loader_command"].append("3")
        self.post("/calibrate/run", {"owner": "alice"})
        job = self.wait_job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("exited with 3", job["error"])
        self.assertIsNone(self.mgr.lease)
        self.assertEqual(self.analyzed, [])

    def test_one_at_a_time(self):
        self.post("/calibrate/run", {"owner": "alice"})
        self.assertEqual(self.post("/calibrate/run", {"owner": "bob"})[0], 409)
        self.wait_job()

    def test_queues_behind_the_holder(self):
        token = self.grant("bob", "game")
        self.post("/calibrate/run", {"owner": "alice"})
        time.sleep(1)
        self.assertEqual(self.mgr.calibrator.job["state"], "queued")
        self.assertEqual([q["owner"] for q in self.mgr.status()["queue"]], ["alice"])
        self.mgr.release(token)
        self.assertEqual(self.wait_job()["state"], "done")

    def test_elf_is_served_for_the_guided_flow(self):
        code, ctype, data = self.request("/calibrate/calib.elf", token=None)
        self.assertEqual((code, data[:4]), (200, b"\x7fELF"))


if __name__ == "__main__":
    unittest.main()
