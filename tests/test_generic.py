"""The guide and GUI describe the rig as configured: site naming, loaders dcmgr can't check (probe: none),
no power control, power commands, capture and recordings."""
import json
import sys
import time
import unittest

from rig import RigCase, srv, wait_for
from test_http import HttpCase


def guide(mgr):
    return srv.render_guide(mgr, "http://rig")


class GuideTests(RigCase):
    def test_default_rig(self):
        g = guide(self.mgr)
        self.assertTrue(g.startswith("# Dreamcast rig (dcmgr)"))
        self.assertIn("running dcload-ip", g)
        self.assertIn("available through Home Assistant", g)
        self.assertIn("Never run `dc-tool-ip`", g)
        self.assertIn("curl -fsSL http://rig/dcm.py", g)       # Linux/macOS setup
        self.assertIn("Invoke-WebRequest http://rig/dcm.py", g)  # Windows setup
        self.assertNotIn("wget", g)
        self.assertNotIn("## Screen capture", g)  # no capture device configured here
        self.assertNotIn("## Recordings", g)
        for site_specific in ("31kHz", "HDMI", "smart plug", "BBA"):
            self.assertNotIn(site_specific, g)

    def test_site_name_and_description(self):
        self.cfg.update(site_name="Lab DC #2", site_description="Under the desk in room 4; PAL console.")
        g = guide(self.mgr)
        self.assertTrue(g.startswith("# Lab DC #2 (dcmgr)\n\nUnder the desk in room 4; PAL console."))
        self.assertEqual(self.mgr.status()["config"]["site_name"], "Lab DC #2")

    def test_without_power_control(self):
        self.cfg["ha_url"] = ""
        g = guide(self.mgr)
        self.assertIn("NOT available (no power control configured)", g)
        self.assertIn("it has no power control here", g)
        self.assertIn("nothing checks the DC for you here", g)

    def test_power_command_is_named(self):
        self.cfg["power_command"] = [sys.executable, "-c", "pass"]
        self.assertIn("available through power command", guide(self.mgr))

    def test_capture_and_recordings_sections(self):
        self.cfg["capture_input"] = ["-f", "lavfi", "-i", "testsrc"]
        m = self.new_manager()
        g = guide(m)
        self.assertIn("## Screen capture", g)
        self.assertIn("the capture chain doesn't show", g)
        self.assertNotIn("Power: None", g)
        self.assertIn("## Recordings: what the screen showed at each console line", g)
        self.assertIn("http://rig/sessions/<id>/frame.jpg?t=<t>", g)


class ProbeNoneTests(RigCase):
    config = {"probe": "none", "dc_ip": "", "recover_on_release": True}

    def test_prepare_needs_no_network(self):
        self.dc.close()  # nothing answers anywhere: it doesn't matter
        token = self.grant()
        start = time.time()
        r = self.mgr.prepare(token)
        self.assertLess(time.time() - start, 1.0)
        self.assertEqual((r["ip"], r["version"]), ("", "not checked"))
        self.assertEqual(self.mgr.status()["dc"]["state"], "unchecked")

    def test_release_never_resets(self):
        token = self.grant()
        self.mgr.prepare(token)
        self.mgr.release(token)
        time.sleep(1.5)
        self.assertEqual(self.dc.calls, [])

    def test_manual_reset_still_works(self):
        self.mgr.reset(None, by="admin")
        self.assertEqual(self.dc.calls, ["turn_off", "turn_on"])

    def test_guide(self):
        g = guide(self.mgr)
        self.assertIn("a loader this server doesn't talk to directly", g)
        self.assertIn("Loader: not checked by dcmgr", g)
        self.assertIn("`dcm run -- <loader> ...`", g)
        self.assertNotIn("Never run `dc-tool-ip`", g)


class ProbeNoneClientTests(HttpCase):
    config = {"api_token": "secret", "probe": "none", "dc_ip": ""}

    def test_dcm_run(self):
        import test_client
        r = test_client.ClientTests.dcm(self, "run", "--", sys.executable, "-c", "print('serial ok')")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DC ready (the server doesn't check this loader)", r.stderr)
        self.assertIn("serial ok", r.stdout)


class ConfigValidationTests(unittest.TestCase):
    def test_bad_probe_is_refused(self):
        import os
        import subprocess
        import tempfile
        from rig import ROOT
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.json")
            with open(path, "w") as f:
                json.dump({"probe": "telepathy", "state_file": os.path.join(d, "s.json")}, f)
            r = subprocess.run([sys.executable, os.path.join(ROOT, "dcmgr_server.py"), "-c", path],
                               capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("probe must be vers, ping or none", r.stderr)


if __name__ == "__main__":
    unittest.main()
