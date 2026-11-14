"""Structured events: what each action records, filtering, the /events endpoint, old log files."""
import json
import os
import time
import unittest

from rig import RigCase, srv, wait_for
from test_http import HttpCase


class EventRecordTests(RigCase):
    def test_every_record_has_the_core_fields(self):
        self.mgr.release(self.grant())
        for e in self.mgr.events:
            self.assertEqual(set(e) >= {"t", "kind", "level", "console", "msg"}, True, e)
            self.assertEqual(e["console"], "dc1")

    def test_lease_lifecycle(self):
        token = self.grant("alice", "game")
        e = self.last("lease.granted")
        self.assertEqual((e["owner"], e["project"], e["level"]), ("alice", "game", "info"))
        self.assertEqual(e["msg"], "lease granted to alice (game)")  # wording unchanged for the GUI
        self.mgr.release(token)
        e = self.last("lease.released")
        self.assertEqual((e["owner"], e["project"]), ("alice", "game"))
        self.assertGreaterEqual(e["duration"], 0)
        self.assertNotIn("by", e)  # None fields are dropped
        h = self.mgr.history[-1]
        self.assertEqual((h["outcome"], h["by"], h["console"]), ("released", None, "dc1"))

    def test_vacate_records_who_and_why(self):
        self.grant("alice", "game")
        self.mgr.release(None, force=True, by="bob", reason="stuck", drain=False)
        e = self.last("lease.vacated")
        self.assertEqual((e["owner"], e["by"], e["reason"], e["level"]), ("alice", "bob", "stuck", "warning"))
        h = self.mgr.history[-1]
        self.assertEqual((h["outcome"], h["by"], h["reason"]), ("vacated", "bob", "stuck"))

    def test_expiry(self):
        self.grant("alice", "game")
        self.assertTrue(wait_for(lambda: self.mgr.status()["lease"] is None, timeout=4))
        self.assertEqual(self.last("lease.expired")["level"], "warning")
        self.assertEqual(self.mgr.history[-1]["outcome"], "expired")

    def test_holds(self):
        token = self.grant("bob")
        self.mgr.hold("alice", 15)
        self.assertEqual(self.last("hold.queued")["position"], 1)
        self.mgr.release(token)
        self.mgr.status()
        self.assertEqual(self.last("hold.granted")["minutes"], 15)
        self.mgr.extend("carol", 5)
        e = self.last("hold.extended")
        self.assertEqual((e["owner"], e["by"], e["minutes"]), ("alice", "carol", 5))
        self.mgr.lease["expires"] = time.time() - 1
        self.mgr.status()
        self.assertEqual(self.last("hold.ended")["owner"], "alice")

    def test_queue_nudge_and_maintenance(self):
        self.grant("alice", "game")
        self.queue("bob", "other")
        self.queue("carol", "other")
        qid = self.mgr.status()["queue"][1]["id"]
        self.mgr.move(qid, 0, "admin")
        self.assertEqual(self.last("queue.moved")["position"], 1)
        self.mgr.evict(qid, "admin")
        e = self.last("queue.evicted")
        self.assertEqual((e["owner"], e["project"], e["by"]), ("carol", "other", "admin"))
        self.mgr.nudge("bob", "please")
        e = self.last("lease.nudged")
        self.assertEqual((e["owner"], e["project"], e["by"], e["message"]), ("alice", "game", "bob", "please"))
        self.mgr.set_maintenance(True, "admin", "disc swap")
        self.assertEqual(self.last("maintenance.on")["note"], "disc swap")
        self.mgr.set_maintenance(False, "admin")
        self.assertIn("maintenance.off", self.kinds())

    def test_power_cycle_is_tagged_with_the_session_it_hit(self):
        token = self.grant("alice", "game")
        self.dc.frozen = True
        self.mgr.prepare(token)
        cycle, back = self.last("power.cycle"), self.last("power.back")
        self.assertEqual((cycle["owner"], cycle["project"], cycle["manual"]), ("alice", "game", False))
        self.assertEqual(back["ip"], self.ips[1])
        e = self.last("dc.ip_changed")
        self.assertEqual((e["old_ip"], e["ip"]), (self.ips[0], self.ips[1]))

    def test_failed_reset_is_an_error(self):
        self.cfg["max_resets_per_hour"] = 0
        token = self.grant("alice", "game")
        self.dc.frozen = True
        with self.assertRaises(srv.ApiError):
            self.mgr.prepare(token)
        e = self.last("power.failed")
        self.assertEqual((e["level"], e["owner"]), ("error", "alice"))

    def test_manual_power(self):
        self.mgr.power(False, "admin")
        self.assertEqual(self.last("power.off")["by"], "admin")

    def test_console_id_is_configurable(self):
        self.cfg["console_id"] = "dc-lab"
        self.grant()
        self.assertEqual(self.last("lease.granted")["console"], "dc-lab")


class QueryTests(RigCase):
    config = {"retention_days": 0}  # the fixtures are timestamped 1970

    def setUp(self):
        super().setUp()
        recs = [
            ("lease.granted", "info", "lease granted to alice (game)", {"owner": "alice", "project": "game"}),
            ("lease.nudged", "info", "bob asked alice to release the DC",
             {"owner": "alice", "project": "game", "by": "bob"}),
            ("power.cycle", "warning", "power-cycling DC", {"owner": "alice", "project": "game"}),
            ("power.failed", "error", "DC did not come back", {}),
            ("lease.granted", "info", "lease granted to carol (demo)", {"owner": "carol", "project": "demo"}),
        ]
        # straight into the store with distinct times (the clock may not tick between event() calls)
        for i, (kind, level, msg, extra) in enumerate(recs):
            self.mgr.store.add_event(dict(t=1000.0 + i, kind=kind, level=level, console="dc1", msg=msg, **extra))

    def q(self, **kw):
        return [e["msg"] for e in self.mgr.query_events(**kw)]

    def test_no_filter_is_oldest_first(self):
        self.assertEqual(len(self.q()), 5)
        self.assertEqual(self.q()[0], "lease granted to alice (game)")

    def test_kind_exact_or_prefix(self):
        self.assertEqual(len(self.q(kind="lease")), 3)
        self.assertEqual(len(self.q(kind="lease.granted")), 2)
        self.assertEqual(self.q(kind="leas"), [])  # prefix means a whole dotted segment
        self.assertEqual(len(self.q(kind="power,lease.nudged")), 3)  # several kinds are ORed
        self.assertEqual(len(self.q(kind="power, ,")), 2)

    def test_user_matches_owner_or_actor(self):
        self.assertEqual(self.q(user="bob"), ["bob asked alice to release the DC"])
        self.assertEqual(len(self.q(user="alice")), 3)

    def test_project_level_text(self):
        self.assertEqual(self.q(project="demo"), ["lease granted to carol (demo)"])
        self.assertEqual(self.q(level="warning"), ["power-cycling DC", "DC did not come back"])
        self.assertEqual(self.q(level="error"), ["DC did not come back"])
        self.assertEqual(self.q(q="CAROL"), ["lease granted to carol (demo)"])

    def test_limit_keeps_newest(self):
        self.assertEqual(self.q(limit=2), ["DC did not come back", "lease granted to carol (demo)"])

    def test_time_window(self):
        self.assertEqual(len(self.q(since=1002)), 3)
        self.assertEqual(len(self.q(until=1001)), 2)


class QueryEdgeTests(RigCase):
    def test_like_wildcards_in_kind_are_literal(self):
        self.mgr.event("lease.granted", "x")
        self.assertEqual(self.mgr.query_events(kind="%"), [])
        self.assertEqual(self.mgr.query_events(kind="l_ase"), [])

    def test_queries_reach_past_the_in_memory_buffer(self):
        for i in range(300):
            self.mgr.store.add_event({"t": 1000.0 + i, "kind": "test.bulk", "level": "info", "console": "dc1",
                                      "msg": f"bulk {i}"})
        self.assertEqual(len(self.mgr.query_events(kind="test", limit=1000)), 300)
        self.assertEqual(self.mgr.query_events(kind="test", limit=1)[0]["msg"], "bulk 299")

    def test_extra_fields_round_trip(self):
        self.mgr.event("power.back", "DC back", ip="10.0.0.1", seconds=28.5)
        e = self.mgr.query_events(kind="power.back")[0]
        self.assertEqual((e["ip"], e["seconds"]), ("10.0.0.1", 28.5))


class StoreTests(RigCase):
    def state_dir_file(self, name):
        return os.path.join(self.tmp, name)

    def fresh_db(self):
        """Point the config at a new database, as on the first start after upgrading."""
        self.cfg["db_file"] = self.state_dir_file("upgraded.db")

    def test_imports_old_event_log_once(self):
        with open(self.state_dir_file("events.jsonl"), "w", encoding="utf-8") as f:
            t = time.time() - 3600
            lines = [json.dumps({"t": t, "msg": "lease granted to old (proj)"}), "not json",
                     json.dumps(["not", "a", "dict"]), json.dumps({"msg": "no time"}),
                     json.dumps({"t": t + 1, "kind": "lease.released", "level": "info", "console": "dc1",
                                 "msg": "new style", "owner": "old"})]
            f.write("".join(line + chr(10) for line in lines))
        self.fresh_db()
        m = self.new_manager()
        self.assertEqual([(e["kind"], e["msg"]) for e in m.events],
                         [("legacy", "lease granted to old (proj)"), ("lease.released", "new style")])
        self.assertEqual(m.events[0]["level"], "info")
        self.assertEqual(m.query_events(user="old")[0]["msg"], "new style")
        m.close()
        self.assertEqual(len(self.new_manager().query_events()), 2)  # not imported twice

    def test_imports_history_from_old_state_file(self):
        now = time.time()
        old = [{"owner": "alice", "project": "game", "manual": False, "start": now - 60, "end": now - 30,
                "how": "vacated by bob (stuck)", "keep": False},
               {"owner": "carol", "project": "hands-on", "manual": True, "start": now - 20, "end": now - 10,
                "how": "ended (hold time up)", "keep": True},
               {"garbage": True}]
        with open(self.cfg["state_file"], "w") as f:
            json.dump({"ip": self.ips[0], "history": old}, f)
        self.fresh_db()
        m = self.new_manager()
        self.assertEqual([(h["owner"], h["outcome"], h["console"]) for h in m.history],
                         [("alice", "vacated", "dc1"), ("carol", "ended", "dc1")])
        self.assertTrue(m.history[1]["keep"])
        m.release(m.acquire("dave", "p")["token"])
        with open(self.cfg["state_file"]) as f:
            self.assertNotIn("history", json.load(f))  # the database owns it now

    def test_sessions_survive_restart_and_filter(self):
        self.mgr.release(self.grant("alice", "game"))
        self.mgr.release(self.grant("bob", "demo"))
        self.grant("carol", "game")
        self.mgr.release(None, force=True, by="alice", drain=False)
        m = self.new_manager()
        self.assertEqual([h["owner"] for h in m.history], ["alice", "bob", "carol"])
        self.assertEqual([h["owner"] for h in m.query_sessions(project="game")], ["alice", "carol"])
        self.assertEqual([h["owner"] for h in m.query_sessions(user="alice")], ["alice", "carol"])
        self.assertEqual([h["owner"] for h in m.query_sessions(outcome="vacated")], ["carol"])
        self.assertEqual([h["owner"] for h in m.query_sessions(limit=1)], ["carol"])

    def test_retention_prunes_old_rows(self):
        old = time.time() - 400 * 86400
        self.mgr.store.add_event({"t": old, "kind": "test.old", "level": "info", "console": "dc1", "msg": "old"})
        self.mgr.store.add_session({"owner": "a", "project": "p", "start": old, "end": old + 1})
        self.mgr.event("test.new", "new")
        m = self.new_manager()  # prunes at startup
        self.assertEqual([e["msg"] for e in m.query_events(kind="test")], ["new"])
        self.assertEqual(m.query_sessions(), [])

    def test_newer_schema_is_refused(self):
        self.mgr.store.db.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
        with self.assertRaises(RuntimeError):
            srv.Store(self.mgr.store.path)


class EventsEndpointTests(HttpCase):
    def get(self, query=""):
        code, _, data = self.request("/events" + query, token=None)
        return code, json.loads(data)

    def test_filters_over_http(self):
        self.mgr.release(self.grant("alice", "game"))
        self.mgr.release(self.grant("bob", "demo"))
        code, r = self.get("?project=game&kind=lease")
        self.assertEqual(code, 200)
        self.assertEqual([e["kind"] for e in r["events"]], ["lease.granted", "lease.released"])
        self.assertEqual(len(self.get("?user=bob&limit=1")[1]["events"]), 1)
        self.assertEqual(len(self.get()[1]["events"]), 4)

    def test_sessions_endpoint(self):
        self.mgr.release(self.grant("alice", "game"))
        self.mgr.release(self.grant("bob", "demo"))
        code, _, data = self.request("/sessions?project=demo", token=None)
        self.assertEqual(code, 200)
        self.assertEqual([h["owner"] for h in json.loads(data)["sessions"]], ["bob"])
        self.assertEqual(self.request("/sessions?since=never", token=None)[0], 400)

    def test_bad_filters(self):
        self.assertEqual(self.get("?level=loud")[0], 400)
        self.assertEqual(self.get("?limit=many")[0], 400)
        self.assertEqual(self.get("?since=yesterday")[0], 400)

    def test_status_events_keep_t_and_msg(self):
        self.grant("alice", "game")
        e = self.mgr.status()["events"][-1]
        self.assertEqual(e["msg"], "lease granted to alice (game)")
        self.assertIsInstance(e["t"], float)


if __name__ == "__main__":
    unittest.main()
