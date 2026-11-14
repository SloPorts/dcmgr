"""End to end: the real dcm.py client, run as a subprocess against an in-process server + fake rig."""
import os
import subprocess
import sys
import unittest

from rig import ROOT, srv, wait_for
from test_http import HttpCase

DCM = os.path.join(ROOT, "dcm.py")
PY = sys.executable


class ClientTests(HttpCase):
    def dcm(self, *args, timeout=60, env=None):
        e = dict(os.environ, DCMGR_URL=self.base, DCMGR_TOKEN="secret",
                 DCMGR_CLIENT_CONFIG=os.path.join(self.tmp, "no-client.json"))
        for k in ("DCMGR_LEASE", "DC_IP"):
            e.pop(k, None)
        e.update(env or {})
        return subprocess.run([PY, DCM, *args], capture_output=True, text=True, timeout=timeout, env=e)

    def test_version(self):
        r = self.dcm("--version")
        self.assertEqual(r.stdout.strip(), f"dcm {srv.__version__}")

    def test_run_passes_ip_and_exit_code_and_releases(self):
        r = self.dcm("run", "-p", "myproj", "--", PY, "-c",
                     "import os, sys; print(os.environ['DC_IP'], sys.argv[1]); sys.exit(3)", "{ip}")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(r.stdout.split(), [self.ips[0], self.ips[0]])
        self.assertIn("lock released", r.stderr)
        self.assertIsNone(self.mgr.status()["lease"])
        self.assertEqual(self.mgr.history[-1]["project"], "myproj")

    def test_nested_dcm_reuses_the_lock(self):
        inner = f"import subprocess, sys; sys.exit(subprocess.call([sys.executable, {DCM!r}, 'ip']))"
        r = self.dcm("run", "--", PY, "-c", inner)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), self.ips[0])
        self.assertEqual(len(self.mgr.history), 1)

    def test_no_wait_fails_when_busy(self):
        self.grant("someone-else", "their-game")
        r = self.dcm("run", "--no-wait", "--", PY, "-c", "pass")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("DC is busy: held by someone-else (their-game)", r.stderr)

    def test_idle_watchdog_kills_and_power_cycles(self):
        r = self.dcm("run", "--idle-timeout", "1", "--", PY, "-c", "import time; time.sleep(30)")
        self.assertEqual(r.returncode, 124, r.stderr)
        self.assertIn("DC presumed frozen", r.stderr)
        self.assertEqual(self.dc.calls, ["turn_off", "turn_on"])

    def test_log_file_gets_output(self):
        log = os.path.join(self.tmp, "run.log")
        r = self.dcm("run", "--log", log, "--", PY, "-c", "print('hello from the dc')")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(log) as f:
            self.assertIn("hello from the dc", f.read())

    def test_status_shows_holder(self):
        self.grant("alice", "game")
        r = self.dcm("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Holder: alice (game)", r.stdout)

    def test_unlock_needs_force(self):
        self.grant("alice")
        self.assertNotEqual(self.dcm("unlock").returncode, 0)
        self.assertIsNotNone(self.mgr.lease)
        r = self.dcm("unlock", "--force", "--keep")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(wait_for(lambda: self.mgr.lease is None, timeout=2))

    def test_unreachable_server(self):
        r = self.dcm("status", env={"DCMGR_URL": "http://127.0.0.1:9"})
        self.assertIn("cannot reach dcmgr", r.stderr)
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
