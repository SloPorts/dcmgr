"""Interactive picture adjustment: crop the capture to centre an off-centre image."""
import json
import shutil
import struct
import time
import unittest

from rig import RigCase, srv
from test_http import HttpCase

ApiError = srv.ApiError
from dcmgr.capture import crop_from_filter  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
TESTSRC = ["-re", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=15"]


def jpeg_size(data):
    """(width, height) from a JPEG's SOF marker."""
    i = 2
    while i < len(data):
        marker, length = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + length
    return None


class FilterParsingTests(unittest.TestCase):
    def test_pixel_crop_from_config(self):
        self.assertEqual(crop_from_filter("crop=1194:692:41:1,scale=640:480", "1280x720"),
                         {"x": 0.03203, "y": 0.00139, "w": 0.93281, "h": 0.96111})

    def test_centred_crop(self):  # ffmpeg centres a crop without x/y
        self.assertEqual(crop_from_filter("crop=960:720,scale=640:480", "1280x720"),
                         {"x": 0.125, "y": 0.0, "w": 0.75, "h": 1.0})

    def test_unknowable(self):
        self.assertIsNone(crop_from_filter("scale=640:480", "1280x720"))
        self.assertIsNone(crop_from_filter("crop=960:720", ""))  # no capture_size: fractions unknown
        self.assertIsNone(crop_from_filter("crop=2000:720", "1280x720"))


class CropTests(RigCase):
    config = {"capture_input": TESTSRC}

    def test_settings_sources(self):
        st = self.mgr.capture_settings()
        self.assertEqual((st["source"], st["crop"], st["adjusting"]), ("none", {"x": 0, "y": 0, "w": 1, "h": 1}, False))
        self.cfg.update(capture_filter="crop=960:720,scale=640:480", capture_size="1280x720")
        st = self.mgr.capture_settings()
        self.assertEqual((st["source"], st["crop"]["x"]), ("config", 0.125))
        self.assertEqual(st["filter"], "crop=960:720,scale=640:480")

    def test_adjust_shows_whole_frame_then_save(self):
        self.cfg["capture_filter"] = "crop=960:720,scale=640:480"
        st = self.mgr.capture_adjust(True, "alice")
        self.assertTrue(st["adjusting"])
        self.assertEqual(self.mgr.capture.filter(), "scale=640:480")
        self.assertTrue(self.mgr.status()["capture"]["adjusting"])
        st = self.mgr.capture_crop({"x": 0.05, "y": 0.02, "w": 0.9, "h": 0.95}, "alice")
        self.assertFalse(st["adjusting"])
        self.assertEqual(st["source"], "interactive")
        self.assertEqual(st["filter"], "crop=w=iw*0.90000:h=ih*0.95000:x=iw*0.05000:y=ih*0.02000,scale=640:480")
        self.assertIn("capture.cropped", self.kinds())
        # kept across restarts
        self.assertEqual(self.new_manager().capture.crop, {"x": 0.05, "y": 0.02, "w": 0.9, "h": 0.95})

    def test_reset_returns_to_config(self):
        self.cfg["capture_filter"] = "crop=960:720,scale=640:480"
        self.mgr.capture_crop({"x": 0, "y": 0, "w": 0.5, "h": 0.5}, "alice")
        st = self.mgr.capture_crop(None, "alice", reset=True)
        self.assertEqual((st["source"], st["filter"]), ("config", "crop=960:720,scale=640:480"))
        self.assertIsNone(self.new_manager().capture.crop)

    def test_validation(self):
        for bad in (None, {"x": 0}, {"x": "a", "y": 0, "w": 1, "h": 1}, {"x": 0.5, "y": 0, "w": 0.6, "h": 1},
                    {"x": 0, "y": 0, "w": 0.05, "h": 1}, {"x": -0.1, "y": 0, "w": 0.5, "h": 0.5}):
            with self.assertRaises(ApiError, msg=bad) as cm:
                self.mgr.capture_crop(bad, "alice")
            self.assertEqual(cm.exception.code, 400)

    def test_forgotten_adjust_mode_times_out(self):
        self.mgr.capture_adjust(True, "alice")
        self.mgr.capture.adjust_until = time.time() - 1
        self.mgr._check_adjust()
        self.assertEqual(self.mgr.capture.adjust_until, 0.0)
        self.assertIn("capture.adjust_timeout", self.kinds())

    def test_cancel(self):
        self.mgr.capture_adjust(True, "alice")
        self.assertFalse(self.mgr.capture_adjust(False, "alice")["adjusting"])
        self.assertIsNone(self.mgr.capture.crop)


class NoCaptureCropTests(RigCase):
    def test_needs_a_capture_device(self):
        for call in (self.mgr.capture_settings, lambda: self.mgr.capture_adjust(True, "a"),
                     lambda: self.mgr.capture_crop({"x": 0, "y": 0, "w": 1, "h": 1}, "a")):
            with self.assertRaises(ApiError) as cm:
                call()
            self.assertEqual(cm.exception.code, 404)


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class CropFfmpegTests(HttpCase):
    """ffmpeg accepts the generated filters, and the output stays the same size in every mode."""
    config = {"api_token": "secret", "capture_input": TESTSRC, "capture_idle_seconds": 2}

    def frame_size(self):
        return jpeg_size(self.mgr.capture.snapshot(timeout=20))

    def test_modes_keep_the_output_size(self):
        self.assertEqual(self.frame_size(), (1280, 720))  # no filter configured: the raw frame
        code, st = self.post("/capture/adjust", {"on": True, "owner": "alice"})
        self.assertEqual((code, st["adjusting"]), (200, True))
        self.assertEqual(self.frame_size(), (640, 480))
        code, st = self.post("/capture/crop", {"crop": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.6}, "owner": "alice"})
        self.assertEqual(code, 200, st)
        self.assertEqual(self.frame_size(), (640, 480))
        self.assertIsNone(self.mgr.capture.error)
        code, _, data = self.request("/capture/settings", token=None)
        self.assertEqual(json.loads(data)["source"], "interactive")
        self.assertEqual(self.post("/capture/crop", {"crop": {"x": 2}})[0], 400)

    def test_restart_waits_for_the_old_ffmpeg(self):
        # a V4L2 device can only be open once: the new ffmpeg must not start before the old one has exited
        self.mgr.capture.snapshot(timeout=20)
        old = self.mgr.capture.proc
        self.assertIsNone(old.poll())
        self.mgr.capture.restart()
        self.assertIsNotNone(old.poll())
        self.assertEqual(jpeg_size(self.mgr.capture.snapshot(timeout=20)), (1280, 720))
        self.assertIsNot(self.mgr.capture.proc, old)


if __name__ == "__main__":
    unittest.main()
