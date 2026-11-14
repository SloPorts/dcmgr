"""power_command: a site-managed script instead of Home Assistant switches the DC's power."""
import os
import sys
import textwrap
import time
import unittest

from rig import RigCase, srv

ApiError = srv.ApiError

# A stand-in for a relay/PDU script: it drives the fake rig the way HA would, and remembers the state.
SCRIPT = textwrap.dedent('''
    import json, os, sys, urllib.request
    action = sys.argv[1]
    state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "power.state")
    if os.path.exists(os.path.join(os.path.dirname(state_file), "jammed")) and action != "state":
        sys.exit("relay jammed")
    if action == "state":
        if not os.path.exists(state_file):
            sys.exit(2)  # this script can't tell yet
        print(open(state_file).read().strip(), "11.5 W")
        sys.exit(0)
    req = urllib.request.Request(f"{HA}/api/services/switch/turn_{action}", data=b"{}", method="POST",
                                 headers={"Authorization": "Bearer test-ha-token"})
    urllib.request.urlopen(req, timeout=5).read()
    open(state_file, "w").write(action)
    assert os.environ["DCMGR_ACTION"] == action and os.environ["DCMGR_CONSOLE"] == "dc1"
''')


class PowerCommandTests(RigCase):
    def setUp(self):
        super().setUp()
        self.script = os.path.join(self.tmp, "power.py")
        with open(self.script, "w") as f:
            f.write(f"HA = {self.cfg['ha_url']!r}\n" + SCRIPT)
        # HA stays configured to show that power_command takes precedence
        self.cfg["power_command"] = [sys.executable, self.script]

    def test_status_reports_the_command(self):
        st = self.mgr.status()
        self.assertEqual((st["power_enabled"], st["power_control"], st["ha_enabled"]), (True, "power command", True))

    def test_power_cycle_through_the_command(self):
        token = self.grant()
        self.dc.frozen = True
        self.assertEqual(self.mgr.prepare(token)["ip"], self.ips[1])
        self.assertEqual(self.dc.calls, ["turn_off", "turn_on"])
        self.assertIn("via power command", self.last("power.cycle")["msg"])

    def test_state_and_power_reading(self):
        self.mgr._poll_plug()
        self.assertEqual(self.mgr.plug["state"], "unknown")  # script exits 2: can't tell
        self.mgr.power(False, "admin")
        self.mgr._poll_plug()
        self.assertEqual((self.mgr.plug["state"], self.mgr.plug["power"], self.mgr.plug["unit"]), ("off", "11.5", "W"))
        self.assertEqual(self.mgr.dc_state, "off")

    def test_failure_is_reported(self):
        open(os.path.join(self.tmp, "jammed"), "w").close()
        token = self.grant()
        self.dc.frozen = True
        with self.assertRaises(ApiError) as cm:
            self.mgr.prepare(token)
        self.assertIn("relay jammed", str(cm.exception))
        self.assertEqual(self.last("power.failed")["level"], "error")
        with self.assertRaises(ApiError) as cm:
            self.mgr.power(True, "admin")
        self.assertEqual(cm.exception.code, 502)

    def test_timeout(self):
        self.cfg["power_command"] = [sys.executable, "-c", "import time; time.sleep(5)"]
        self.cfg["power_command_timeout"] = 0.5
        start = time.time()
        with self.assertRaises(ApiError) as cm:
            self.mgr.power(True, "admin")
        self.assertLess(time.time() - start, 4)
        self.assertIn("timed out", str(cm.exception))

    def test_command_as_a_string(self):
        q = '"' if os.name == "nt" else "'"
        self.cfg["power_command"] = f"{q}{sys.executable}{q} {q}{self.script}{q}"
        self.mgr.power(False, "admin")
        self.assertEqual(self.dc.calls, ["turn_off"])

    def test_missing_command(self):
        self.cfg["power_command"] = [os.path.join(self.tmp, "no-such-script")]
        with self.assertRaises(ApiError) as cm:
            self.mgr.power(True, "admin")
        self.assertIn("power command", str(cm.exception))


class NoPowerTests(RigCase):
    config = {"ha_url": ""}

    def test_without_any_power_control(self):
        st = self.mgr.status()
        self.assertEqual((st["power_enabled"], st["power_control"]), (False, None))
        with self.assertRaises(ApiError) as cm:
            self.mgr.reset(None)
        self.assertEqual(cm.exception.code, 501)
        self.assertIn("power_command", str(cm.exception))
        guide = srv.render_guide(self.mgr, "http://x")
        self.assertIn("no power control configured", guide)


if __name__ == "__main__":
    unittest.main()
