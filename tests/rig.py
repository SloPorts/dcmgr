"""Shared test fixtures: a fake Dreamcast + Home Assistant (tools/fake_rig.py) per test, on its own
loopback /29 so discovery sweeps never see another test's DC, and a Manager configured to use it.

Needs a host where any 127.x.y.z address can be bound (Linux, Windows; not macOS by default).
"""
import itertools
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tools")]

import dcmgr_server as srv  # noqa: E402
import fake_rig  # noqa: E402

fake_rig.print = lambda *a, **k: None  # keep the fake rig quiet during tests
srv.log.disabled = True

_subnets = itertools.count(1)


def wait_for(pred, timeout=10.0, every=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(every)
    return pred()


class RigCase(unittest.TestCase):
    """Each test gets: self.dc (FakeDC at self.ips[0], moving to self.ips[1] on power-cycle),
    self.cfg, self.mgr (a Manager whose monitor thread is not started)."""

    config = {}  # per-class overrides

    def setUp(self):
        n = next(_subnets)
        self.subnet = f"127.0.{n}.0/29"
        self.ips = [f"127.0.{n}.2", f"127.0.{n}.3"]
        self.dc = fake_rig.FakeDC(self.ips, boot_delay=0.3)
        self.ha = fake_rig.make_ha_server(self.dc, port=0)
        threading.Thread(target=self.ha.serve_forever, daemon=True).start()
        self.tmp = tempfile.mkdtemp(prefix="dcmgr-test-")
        self.cfg = dict(srv.DEFAULTS)
        self.cfg.update(
            dc_ip=self.ips[0], subnet=self.subnet, dc_mac="",
            ha_url=f"http://127.0.0.1:{self.ha.server_address[1]}", ha_token="test-ha-token",
            ha_entity="switch.dreamcast",
            power_off_seconds=0, boot_grace=0, boot_timeout=8,
            lease_ttl=2, queue_poll_timeout=2, idle_probe_interval=3600,
            recover_on_release=False,
            state_file=os.path.join(self.tmp, "state.json"),
        )
        self.cfg.update(self.config)
        self._managers = []
        self.mgr = self.new_manager()

    def new_manager(self):
        """A Manager on this test's config (e.g. to simulate a restart); closed in tearDown."""
        m = srv.Manager(self.cfg)
        self._managers.append(m)
        return m

    def tearDown(self):
        for m in self._managers:
            m.close()
        self.ha.shutdown()
        self.ha.server_close()
        self.dc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def grant(self, owner="alice", project="proj"):
        r = self.mgr.acquire(owner, project, wait=0)
        self.assertTrue(r["granted"], r)
        return r["token"]

    def queue(self, owner, project="proj"):
        """Join the queue behind the current holder; returns the ticket."""
        r = self.mgr.acquire(owner, project, wait=0.05)
        self.assertFalse(r["granted"], r)
        return r["ticket"]

    def history_how(self):
        return [h["how"] for h in self.mgr.history]

    def kinds(self):
        return [e["kind"] for e in self.mgr.events]

    def last(self, kind):
        """The most recent event of this kind."""
        return next(e for e in reversed(self.mgr.events) if e["kind"] == kind)

    def event_msgs(self):
        return [e["msg"] for e in self.mgr.events]
