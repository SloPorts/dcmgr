"""Console capture: storing the loader's output per session, reading and following it, dcm console."""
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import unittest

import test_client
from rig import RigCase, srv, wait_for
from test_http import HttpCase

ApiError = srv.ApiError


class ConsoleTests(RigCase):
    def test_lines_are_numbered_and_moved_onto_server_time(self):
        token = self.grant("alice", "game")
        sid = self.mgr.status()["lease"]["id"]
        now = time.time()
        # the client's clock is 1000 s slow; its lines were printed 0.2 and 0.1 s before sending
        r = self.mgr.console_append(token, [[now - 1000.2, "boot"], [now - 1000.1, "hello"]], sent=now - 1000)
        self.assertEqual(r["lines"], 2)
        out = self.mgr.console_read()
        self.assertTrue(out["live"])
        self.assertEqual(out["session"]["uid"], sid)
        self.assertEqual([(l["seq"], l["text"]) for l in out["lines"]], [(1, "boot"), (2, "hello")])
        self.assertAlmostEqual(out["lines"][1]["t"], now - 0.1, delta=0.5)
        self.assertEqual(self.mgr.status()["lease"]["lines"], 2)

    def test_times_are_clamped_to_the_session(self):
        token = self.grant()
        self.mgr.console_append(token, [[0, "ancient"], [time.time() + 999, "future"]], sent=time.time())
        lines = self.mgr.console_read()["lines"]
        self.assertGreaterEqual(lines[0]["t"], self.mgr.lease["acquired"])
        self.assertLessEqual(lines[1]["t"], time.time())

    def test_only_the_holder_can_append(self):
        self.grant()
        with self.assertRaises(ApiError) as cm:
            self.mgr.console_append("bogus", [[time.time(), "x"]])
        self.assertEqual(cm.exception.code, 409)
        token = self.mgr.lease["token"]
        for bad in ("nope", [["x"]], [[1, "a"], 5]):
            with self.assertRaises(ApiError):
                self.mgr.console_append(token, bad)

    def test_finished_sessions_and_defaults(self):
        token = self.grant("alice", "game")
        sid = self.mgr.lease["id"]
        self.mgr.console_append(token, [[time.time(), "alice's output"]])
        self.mgr.release(token)
        self.mgr.release(self.grant("bob", "other"))  # no output: not "latest with output"
        h = self.mgr.history[-2]
        self.assertEqual((h["uid"], h["lines"]), (sid, 1))
        out = self.mgr.console_read()
        self.assertEqual((out["session"]["uid"], out["live"]), (sid, False))
        self.assertEqual(self.mgr.console_read(user="alice")["lines"][0]["text"], "alice's output")
        with self.assertRaises(ApiError) as cm:
            self.mgr.console_read(user="bob")
        self.assertEqual(cm.exception.code, 404)
        with self.assertRaises(ApiError):
            self.mgr.console_read(session="0" * 12)
        released = [e for e in self.mgr.events if e["kind"] == "lease.released"]
        self.assertEqual([(e["owner"], e["session"], e.get("lines")) for e in released][0], ("alice", sid, 1))

    def test_live_session_of_someone_else_is_not_the_default_for_you(self):
        token = self.grant("alice", "game")
        self.mgr.console_append(token, [[time.time(), "a1"]])
        self.mgr.release(token)
        bob = self.grant("bob", "other")
        self.mgr.console_append(bob, [[time.time(), "b1"]])
        self.assertEqual(self.mgr.console_read(user="alice")["lines"][0]["text"], "a1")
        self.assertEqual(self.mgr.console_read()["lines"][0]["text"], "b1")

    def test_long_poll_wakes_on_new_lines_and_on_release(self):
        token = self.grant()
        got = {}

        def reader():
            got["r"] = self.mgr.console_read(wait=10)

        t = threading.Thread(target=reader)
        t.start()
        time.sleep(0.3)
        start = time.time()
        self.mgr.console_append(token, [[time.time(), "late"]])
        t.join(5)
        self.assertLess(time.time() - start, 2)
        self.assertEqual([l["text"] for l in got["r"]["lines"]], ["late"])

        def reader2():
            got["r2"] = self.mgr.console_read(session=self.mgr.lease["id"], after=1, wait=10)

        t = threading.Thread(target=reader2)
        t.start()
        time.sleep(0.3)
        self.mgr.release(token)
        t.join(5)
        self.assertFalse(got["r2"]["live"])
        self.assertEqual(got["r2"]["lines"], [])
        self.assertIsNotNone(got["r2"]["session"]["end"])

    def test_paging_and_tail(self):
        token = self.grant()
        self.mgr.console_append(token, [[time.time(), f"line {i}"] for i in range(1, 51)])
        self.assertEqual([l["seq"] for l in self.mgr.console_read(after=45)["lines"]], [46, 47, 48, 49, 50])
        self.assertEqual([l["seq"] for l in self.mgr.console_read(limit=3)["lines"]], [1, 2, 3])
        self.assertEqual([l["seq"] for l in self.mgr.console_read(limit=3, tail=True)["lines"]], [48, 49, 50])

    def test_per_session_cap(self):
        self.cfg["console_max_lines"] = 5
        token = self.grant()
        r = self.mgr.console_append(token, [[time.time(), str(i)] for i in range(8)])
        self.assertEqual(r["lines"], 5)
        self.mgr.console_append(token, [[time.time(), "more"]])
        self.assertEqual(self.kinds().count("console.truncated"), 1)
        self.assertEqual(len(self.mgr.console_read()["lines"]), 5)

    def test_long_lines_are_cut(self):
        token = self.grant()
        self.mgr.console_append(token, [[time.time(), "x" * 10000]])
        self.assertEqual(len(self.mgr.console_read()["lines"][0]["text"]), 4000)

    def test_restart_keeps_numbering(self):
        token = self.grant()
        self.mgr.console_append(token, [[time.time(), "a"], [time.time(), "b"]])
        m2 = self.new_manager()
        self.assertEqual(m2.lease["lines"], 2)
        self.assertEqual(m2.console_append(token, [[time.time(), "c"]])["lines"], 3)

    def test_pinned_sessions_keep_console_through_retention(self):
        old = time.time() - 400 * 86400
        st = self.mgr.store
        for uid, pinned in (("aaaaaaaaaaaa", True), ("bbbbbbbbbbbb", False)):
            st.add_session({"owner": "a", "project": "p", "start": old, "end": old + 1, "uid": uid,
                            "lines": 1, "pinned": pinned})
            st.add_console(uid, [(1, old, "text")])
        self.new_manager()  # prunes at startup
        self.assertEqual(len(st.console("aaaaaaaaaaaa")), 1)
        self.assertEqual(st.console("bbbbbbbbbbbb"), [])
        self.assertIsNotNone(st.session("aaaaaaaaaaaa"))

    def test_v2_database_is_upgraded(self):
        path = os.path.join(self.tmp, "v2.db")
        db = sqlite3.connect(path)
        db.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO meta VALUES ('schema_version', '2');
            CREATE TABLE sessions (id INTEGER PRIMARY KEY, console TEXT, owner TEXT, project TEXT,
                manual INTEGER NOT NULL, started REAL NOT NULL, ended REAL NOT NULL, outcome TEXT, how TEXT,
                actor TEXT, reason TEXT, keep INTEGER NOT NULL);
            INSERT INTO sessions (owner, project, manual, started, ended, outcome, how, keep)
                VALUES ('alice', 'game', 0, 1, 2, 'released', 'released', 0);""")
        db.commit()
        db.close()
        st = srv.Store(path)
        try:
            self.assertEqual(st.meta("schema_version"), str(srv.SCHEMA_VERSION))
            old = st.sessions()[0]
            self.assertEqual((old["owner"], old["uid"], old["lines"], old["pinned"]), ("alice", None, 0, False))
            st.add_session({"owner": "b", "project": "p", "start": 3, "end": 4, "uid": "cccccccccccc", "lines": 2})
            self.assertEqual(st.session("cccccccccccc")["lines"], 2)
        finally:
            st.close()


class ConsoleHttpTests(HttpCase):
    def test_append_and_read_over_http(self):
        _, r = self.post("/acquire", {"owner": "alice", "project": "p"})
        code, a = self.post("/console", {"token": r["token"], "lines": [[time.time(), "hi"]], "sent": time.time()})
        self.assertEqual((code, a["lines"]), (200, 1))
        self.assertEqual(self.post("/console", {"token": "bad", "lines": []})[0], 409)
        code, _, data = self.request("/console?tail=1&limit=5", token=None)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(data)["lines"][0]["text"], "hi")
        self.assertEqual(self.request("/console?after=lots", token=None)[0], 400)
        self.assertEqual(self.request("/console?session=nope", token=None)[0], 404)


class ConsoleClientTests(HttpCase):
    dcm = test_client.ClientTests.dcm
    PRINTER = ("import sys, time\n"
               "print('first line', flush=True)\n"
               "time.sleep(0.3)\n"
               "print('second line\\r', flush=True)\n"
               "sys.stdout.write('no newline at the end'); sys.stdout.flush()\n")

    def test_run_uploads_and_console_reads_back(self):
        r = self.dcm("run", "-p", "game", "--", sys.executable, "-c", self.PRINTER)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("first line", r.stdout)  # still printed locally
        self.assertIn("console output saved (3 lines)", r.stderr)
        h = self.mgr.history[-1]
        self.assertEqual(h["lines"], 3)
        r = self.dcm("console")
        self.assertEqual(r.stdout.splitlines(), ["first line", "second line", "no newline at the end"])
        self.assertIn(f"session {h['uid']}", r.stderr)
        r = self.dcm("console", "--json", "-n", "1")
        self.assertEqual(json.loads(r.stdout)["text"], "no newline at the end")
        r = self.dcm("console", "-t")
        first, second = (float(l.split()[0]) for l in r.stdout.splitlines()[:2])
        self.assertGreaterEqual(second - first, 0.25)  # line times are when each line was printed

    def test_no_console(self):
        r = self.dcm("run", "--no-console", "--", sys.executable, "-c", "print('private')")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.mgr.history[-1]["lines"], 0)
        self.assertNotEqual(self.dcm("console").returncode, 0)

    def test_follow_a_live_session(self):
        slow = "import time\nfor i in range(4):\n    print('tick', i, flush=True)\n    time.sleep(0.4)\n"
        env = dict(os.environ, DCMGR_URL=self.base, DCMGR_TOKEN="secret",
                   DCMGR_CLIENT_CONFIG=os.path.join(self.tmp, "none.json"))
        env.pop("DCMGR_LEASE", None)
        runner = subprocess.Popen([sys.executable, test_client.DCM, "run", "--", sys.executable, "-c", slow],
                                  env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(wait_for(lambda: self.mgr.lease and self.mgr.lease.get("lines"), timeout=15))
            r = self.dcm("console", "-f", "--any", timeout=60)
            self.assertEqual(r.stdout.splitlines(), [f"tick {i}" for i in range(4)])
        finally:
            runner.wait(30)

    def test_old_server_without_console_is_silent(self):
        # a 404 from POST /console (an older dcmgr) must not produce warnings or fail the run
        orig = self.mgr.console_append
        self.mgr.console_append = lambda *a, **k: (_ for _ in ()).throw(ApiError(404, "not found"))
        try:
            r = self.dcm("run", "--", sys.executable, "-c", "print('x')")
        finally:
            self.mgr.console_append = orig
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("console", r.stderr)


if __name__ == "__main__":
    unittest.main()
