"""Manager behaviour: locking, queue, holds, maintenance, discovery, power-cycling, persistence."""
import json
import time
import unittest

from rig import RigCase, srv, wait_for

ApiError = srv.ApiError


class LockTests(RigCase):
    def test_free_dc_is_granted(self):
        r = self.mgr.acquire("alice", "proj", wait=0)
        self.assertTrue(r["granted"])
        self.assertEqual(r["ip"], self.ips[0])
        self.assertEqual(self.mgr.status()["lease"]["owner"], "alice")

    def test_busy_without_wait_does_not_queue(self):
        self.grant("alice")
        r = self.mgr.acquire("bob", "other", wait=0)
        self.assertFalse(r["granted"])
        self.assertIsNone(r["position"])
        self.assertEqual(r["holder"]["owner"], "alice")
        self.assertEqual(self.mgr.status()["queue"], [])

    def test_queue_is_fifo_and_release_hands_over(self):
        token = self.grant("alice")
        bob, carol = self.queue("bob"), self.queue("carol")
        self.assertEqual([q["owner"] for q in self.mgr.status()["queue"]], ["bob", "carol"])
        self.mgr.release(token)
        self.assertFalse(self.mgr.acquire("carol", "proj", carol, wait=0.05)["granted"])
        self.assertTrue(self.mgr.acquire("bob", "proj", bob, wait=0.05)["granted"])
        self.assertEqual(self.history_how(), ["released"])

    def test_release_with_wrong_token_is_refused(self):
        self.grant()
        with self.assertRaises(ApiError) as cm:
            self.mgr.release("not-the-token")
        self.assertEqual(cm.exception.code, 409)
        self.assertIsNotNone(self.mgr.lease)

    def test_heartbeat_extends_and_missed_heartbeats_expire(self):
        token = self.grant()
        before = self.mgr.lease["expires"]
        time.sleep(0.1)
        self.assertGreater(self.mgr.heartbeat(token)["expires"], before)
        self.assertTrue(wait_for(lambda: self.mgr.status()["lease"] is None, timeout=4))
        with self.assertRaises(ApiError) as cm:
            self.mgr.heartbeat(token)
        self.assertEqual(cm.exception.code, 409)
        self.assertIn("expired", self.history_how()[-1])

    def test_vacate_tells_holder_and_drains_before_next_grant(self):
        token = self.grant("alice")
        self.mgr.release(None, force=True, by="admin", reason="stuck")
        r = self.mgr.acquire("bob", "proj", wait=0)
        self.assertFalse(r["granted"])
        self.assertTrue(r["draining"])
        with self.assertRaises(ApiError) as cm:
            self.mgr.heartbeat(token)
        self.assertIn("force-released by admin: stuck", str(cm.exception))
        # once the old holder has heard, the drain shortens to ~2s
        self.assertTrue(wait_for(lambda: self.mgr.acquire("bob", "proj", wait=0)["granted"], timeout=4))
        self.assertEqual(self.history_how()[0], "vacated by admin (stuck)")

    def test_evicted_waiter_is_told_why(self):
        self.grant("alice")
        ticket = self.queue("bob")
        qid = self.mgr.status()["queue"][0]["id"]
        self.mgr.evict(qid, "admin")
        with self.assertRaises(ApiError) as cm:
            self.mgr.acquire("bob", "proj", ticket, wait=0.05)
        self.assertEqual(cm.exception.code, 410)
        self.assertIn("removed from the DC queue by admin", str(cm.exception))

    def test_move_reorders_queue(self):
        self.grant("alice")
        self.queue("bob")
        self.queue("carol")
        carol = self.mgr.status()["queue"][1]["id"]
        self.assertEqual(self.mgr.move(carol, 0, "admin")["position"], 1)
        self.assertEqual([q["owner"] for q in self.mgr.status()["queue"]], ["carol", "bob"])

    def test_unknown_queue_entry(self):
        with self.assertRaises(ApiError) as cm:
            self.mgr.evict("nope", "admin")
        self.assertEqual(cm.exception.code, 404)

    def test_nudge_reaches_holder_once(self):
        token = self.grant("alice")
        self.mgr.nudge("bob", "need it for a demo")
        notices = self.mgr.heartbeat(token)["notices"]
        self.assertEqual([(n["by"], n["msg"]) for n in notices], [("bob", "need it for a demo")])
        self.assertEqual(self.mgr.heartbeat(token)["notices"], [])
        self.assertEqual(len(self.mgr.status()["lease"]["asks"]), 1)

    def test_maintenance_pauses_new_grants(self):
        self.mgr.set_maintenance(True, "admin", "swapping GDEMU card")
        r = self.mgr.acquire("alice", "proj", wait=0)
        self.assertFalse(r["granted"])
        self.assertEqual(r["maintenance"]["note"], "swapping GDEMU card")
        self.mgr.set_maintenance(False, "admin")
        self.assertTrue(self.mgr.acquire("alice", "proj", wait=0)["granted"])


class PauseTests(RigCase):
    def test_pause_now_lets_the_current_session_finish(self):
        token = self.grant("alice")
        ticket = self.queue("bob")
        r = self.mgr.set_maintenance(True, "admin", "disc swap")
        self.assertEqual((r["maintenance"]["mode"], r["maintenance"]["remaining"]), ("now", 0))
        self.assertTrue(self.mgr.heartbeat(token)["ok"])  # alice is not cut short
        self.mgr.release(token)
        r = self.mgr.acquire("bob", "proj", ticket, wait=0.05)
        self.assertFalse(r["granted"])
        self.assertEqual(r["maintenance"]["note"], "disc swap")
        self.mgr.set_maintenance(False, "admin")
        self.assertTrue(self.mgr.acquire("bob", "proj", ticket, wait=0.05)["granted"])

    def test_pause_after_queue_serves_those_already_waiting(self):
        token = self.grant("alice")
        bob = self.queue("bob")
        self.mgr.hold("carol", 15)
        r = self.mgr.set_maintenance(True, "admin", mode="after_queue")
        self.assertEqual(r["maintenance"]["remaining"], 2)
        self.assertIn("after the 2 waiting", self.last("maintenance.on")["msg"])
        dave = self.queue("dave")  # arrives after the pause: waits
        r = self.mgr.acquire("dave", "proj", dave, wait=0.05)
        self.assertEqual(r["maintenance"]["mode"], "after_queue")
        r = self.mgr.acquire("bob", "proj", bob, wait=0.05)
        self.assertIsNone(r["maintenance"])  # bob is still going to get a turn
        self.mgr.release(token)
        self.assertTrue(self.mgr.acquire("bob", "proj", bob, wait=0.05)["granted"])
        self.mgr.release(self.mgr.lease["token"])
        self.assertEqual(self.mgr.status()["lease"]["owner"], "carol")  # server-granted hold
        self.mgr.release(None, force=True, by="carol", drain=False)
        st = self.mgr.status()
        self.assertIsNone(st["lease"])
        self.assertEqual(st["maintenance"]["remaining"], 0)
        self.assertFalse(self.mgr.acquire("dave", "proj", dave, wait=0.05)["granted"])
        self.mgr.set_maintenance(False, "admin")
        self.assertTrue(self.mgr.acquire("dave", "proj", dave, wait=0.05)["granted"])

    def test_late_arrival_moved_to_front_still_waits(self):
        self.grant("alice")
        self.queue("bob")
        self.mgr.set_maintenance(True, "admin", mode="after_queue")
        dave = self.queue("dave")
        self.mgr.move(self.mgr.status()["queue"][1]["id"], 0, "admin")
        self.mgr.release(self.mgr.lease["token"])
        self.assertFalse(self.mgr.acquire("dave", "proj", dave, wait=0.05)["granted"])
        self.assertEqual(self.mgr._next_entry()["owner"], "bob")
        self.assertEqual([(q["owner"], q["waits"]) for q in self.mgr.status()["queue"]],
                         [("dave", True), ("bob", False)])

    def test_bad_mode(self):
        with self.assertRaises(ApiError):
            self.mgr.set_maintenance(True, "admin", mode="later")

    def test_old_maintenance_state_loads(self):
        with open(self.cfg["state_file"], "w") as f:
            json.dump({"maintenance": {"by": "x", "note": "", "since": time.time()}}, f)
        m = self.new_manager()
        self.assertEqual(m.status()["maintenance"]["mode"], "now")
        self.assertFalse(m.acquire("alice", "p", wait=0)["granted"])


class HoldTests(RigCase):
    def test_hold_on_free_dc_is_granted_at_once(self):
        self.assertTrue(self.mgr.hold("alice", 30)["granted"])
        lease = self.mgr.status()["lease"]
        self.assertTrue(lease["manual"])
        self.assertEqual(lease["project"], "hands-on")
        self.assertAlmostEqual(lease["expires"] - time.time(), 30 * 60, delta=5)

    def test_duplicate_holds_are_refused(self):
        self.mgr.hold("alice", 30)
        with self.assertRaises(ApiError):
            self.mgr.hold("alice", 30)

    def test_queued_hold_is_granted_by_server_after_release(self):
        token = self.grant("bob")
        self.assertFalse(self.mgr.hold("alice", 15)["granted"])
        self.mgr.release(token)
        self.assertEqual(self.mgr.status()["lease"]["owner"], "alice")

    def test_extend_and_time_up(self):
        self.mgr.hold("alice", 15)
        before = self.mgr.lease["expires"]
        self.mgr.extend("alice", 10)
        self.assertAlmostEqual(self.mgr.lease["expires"] - before, 600, delta=1)
        self.mgr.lease["expires"] = time.time() - 1
        self.assertIsNone(self.mgr.status()["lease"])
        self.assertEqual(self.history_how(), ["ended (hold time up)"])

    def test_extend_needs_a_hold(self):
        self.grant()
        with self.assertRaises(ApiError):
            self.mgr.extend("alice", 10)


class HardwareTests(RigCase):
    def test_prepare_finds_dc(self):
        token = self.grant()
        r = self.mgr.prepare(token)
        self.assertEqual(r["ip"], self.ips[0])
        self.assertIn("fake", r["version"])
        self.assertTrue(self.mgr.lease["prepared"])
        self.assertEqual(self.dc.calls, [])

    def test_prepare_follows_dc_to_new_ip(self):
        token = self.grant()
        self.dc.move()
        self.assertEqual(self.mgr.prepare(token)["ip"], self.ips[1])
        self.assertIn(f"DC IP changed {self.ips[0]} -> {self.ips[1]}", self.event_msgs())

    def test_frozen_dc_is_power_cycled_and_found_at_new_ip(self):
        token = self.grant()
        self.dc.frozen = True
        self.assertEqual(self.mgr.prepare(token)["ip"], self.ips[1])
        self.assertEqual(self.dc.calls, ["turn_off", "turn_on"])
        self.assertEqual(self.mgr.status()["resets_last_hour"], 1)

    def test_no_reset_when_disallowed(self):
        token = self.grant()
        self.dc.frozen = True
        with self.assertRaises(ApiError) as cm:
            self.mgr.prepare(token, allow_reset=False)
        self.assertEqual(cm.exception.code, 503)
        self.assertEqual(self.dc.calls, [])

    def test_hourly_reset_cap(self):
        self.cfg["max_resets_per_hour"] = 1
        self.mgr.reset_times = [time.time() - 60]
        token = self.grant()
        self.dc.frozen = True
        with self.assertRaises(ApiError) as cm:
            self.mgr.prepare(token)
        self.assertIn("reset suppressed", str(cm.exception))
        self.assertEqual(self.dc.calls, [])

    def test_failed_boots_stop_automatic_resets_but_not_manual(self):
        # lease_ttl: each failed attempt takes several seconds and nobody heartbeats here
        self.cfg.update(max_failed_boots=1, boot_timeout=1, lease_ttl=60)
        token = self.grant()
        self.dc.frozen, self.dc.dead = True, True
        with self.assertRaises(ApiError) as cm:
            self.mgr.prepare(token)
        self.assertIn("did not come back", str(cm.exception))
        self.assertEqual(self.mgr.failed_boots, 1)
        with self.assertRaises(ApiError) as cm:
            self.mgr.prepare(token)
        self.assertIn("automatic reset suppressed", str(cm.exception))
        self.assertEqual(self.dc.calls, ["turn_off", "turn_on"])
        self.dc.dead = False  # disc fixed; a manual reset still goes through
        self.cfg["boot_timeout"] = 8
        self.assertIn("fake", self.mgr.reset(token, by="alice")["version"])
        self.assertEqual(self.mgr.failed_boots, 0)

    def test_reset_of_someone_elses_session_needs_force(self):
        token = self.grant("alice")
        with self.assertRaises(ApiError) as cm:
            self.mgr.reset(None, by="bob")
        self.assertEqual(cm.exception.code, 409)
        self.assertEqual(self.dc.calls, [])
        self.mgr.reset(None, force=True, by="bob")
        self.assertEqual(self.dc.calls, ["turn_off", "turn_on"])
        self.assertEqual(self.history_how(), ["vacated by bob (power-cycled)"])
        with self.assertRaises(ApiError):
            self.mgr.heartbeat(token)

    def test_power_off_while_held_needs_force(self):
        self.grant("alice")
        with self.assertRaises(ApiError) as cm:
            self.mgr.power(False, "bob")
        self.assertEqual(cm.exception.code, 409)
        self.mgr.power(False, "bob", force=True)
        self.assertEqual(self.mgr.status()["plug"]["state"], "off")
        self.assertIsNone(self.mgr.lease)

    def test_powered_off_dc_is_switched_on_by_prepare(self):
        self.mgr.power(False, "admin")
        self.assertEqual(self.mgr.dc_state, "off")
        token = self.grant()
        self.assertEqual(self.mgr.prepare(token)["ip"], self.ips[1])
        self.assertEqual(self.dc.calls[-1], "turn_on")

    def test_without_home_assistant(self):
        self.cfg["ha_url"] = ""
        token = self.grant()
        with self.assertRaises(ApiError) as cm:
            self.mgr.reset(token)
        self.assertEqual(cm.exception.code, 501)
        self.dc.frozen = True
        with self.assertRaises(ApiError) as cm:
            self.mgr.prepare(token)
        self.assertIn("dcload not answering", str(cm.exception))


class RecoveryTests(RigCase):
    config = {"recover_on_release": True}

    def test_release_resets_a_dc_left_running_a_program(self):
        token = self.grant()
        self.mgr.prepare(token)
        self.dc.frozen = True  # the holder's game is still running
        self.mgr.release(token)
        self.assertTrue(wait_for(lambda: self.mgr.dc_state == "ready" and "turn_on" in self.dc.calls,
                                 timeout=15))
        self.assertEqual(self.mgr.ip, self.ips[1])

    def test_keep_skips_recovery(self):
        token = self.grant()
        self.mgr.prepare(token)
        self.dc.frozen = True
        self.mgr.release(token, keep=True)
        time.sleep(1.5)
        self.assertEqual(self.dc.calls, [])
        self.assertTrue(self.mgr.history[-1]["keep"])

    def test_healthy_dc_is_not_reset(self):
        token = self.grant()
        self.mgr.prepare(token)
        self.mgr.release(token)
        self.assertTrue(wait_for(lambda: self.mgr.dc_state == "ready", timeout=5))
        time.sleep(0.5)
        self.assertEqual(self.dc.calls, [])


class PersistenceTests(RigCase):
    def test_state_survives_restart(self):
        self.mgr.release(self.grant("alice"))
        token = self.grant("bob", "game")
        self.mgr.set_maintenance(True, "admin", "after bob")
        self.mgr.event("test.custom", "custom event", project="game")
        m2 = self.new_manager()
        self.assertEqual(m2.lease["owner"], "bob")
        self.assertTrue(m2.heartbeat(token)["ok"])
        self.assertEqual([h["owner"] for h in m2.history], ["alice"])
        self.assertEqual(m2.maintenance["note"], "after bob")
        self.assertEqual(m2.events[-1]["msg"], "custom event")
        self.assertEqual(m2.events[-1]["project"], "game")

    def test_loads_state_from_older_versions(self):
        old_lease = {"token": "t" * 32, "owner": "alice", "project": "p", "acquired": time.time(),
                     "expires": time.time() + 60, "prepared": True}
        with open(self.cfg["state_file"], "w") as f:
            json.dump({"ip": self.ips[1], "lease": old_lease, "reset_times": []}, f)
        m2 = self.new_manager()
        self.assertEqual(m2.ip, self.ips[1])
        self.assertEqual(m2.status()["lease"]["asks"], [])
        self.assertTrue(m2.heartbeat("t" * 32)["ok"])

    def test_corrupt_state_file_is_ignored(self):
        with open(self.cfg["state_file"], "w") as f:
            f.write("{not json")
        m2 = self.new_manager()
        self.assertIsNone(m2.lease)
        self.assertEqual(m2.ip, self.ips[0])


if __name__ == "__main__":
    unittest.main()
