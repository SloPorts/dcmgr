"""Project registry: unique names, ids from .dcm.json, renames, clashes with ad-hoc labels, dcm project."""
import json
import os
import sqlite3
import unittest

import sys

import test_client
from rig import RigCase, srv
from test_http import HttpCase

ApiError = srv.ApiError
PID, PID2 = "a" * 32, "b" * 32


class RegistryTests(RigCase):
    def acquire(self, project, pid=None, owner="alice@vm1"):
        r = self.mgr.acquire(owner, project, wait=0, project_id=pid)
        if r["granted"]:
            self.mgr.release(r["token"], keep=True)
        return r

    def test_ad_hoc_labels_work_as_before(self):
        r = self.acquire("my-game")
        self.assertEqual((r["project"], r["note"]), ("my-game", None))
        self.assertEqual(self.mgr.history[-1]["project"], "my-game")
        self.assertIsNone(self.mgr.store.project(name="my-game"))  # not registered

    def test_ad_hoc_labels_are_cleaned(self):
        self.assertEqual(self.acquire("<b>weird~name!")["project"], "b-weird-name")
        self.assertEqual(self.acquire("game/render")["project"], "game/render")  # sub-projects
        self.assertEqual(self.acquire("/abs/path/")["project"], "abs/path")
        self.assertEqual(self.acquire("")["project"], "unnamed")
        self.assertEqual(len(self.acquire("x" * 80)["project"]), 40)

    def test_register_and_clash(self):
        p = self.mgr.register_project("Game", PID, "alice@vm1", "the platformer")
        self.assertEqual((p["name"], p["owner"], p["description"]), ("Game", "alice@vm1", "the platformer"))
        self.assertEqual(self.mgr.register_project("Game", PID, "bob@vm2")["owner"], "alice@vm1")  # idempotent
        with self.assertRaises(ApiError) as cm:
            self.mgr.register_project("game", PID2, "bob@vm2")  # names are case-insensitive
        self.assertEqual(cm.exception.code, 409)
        self.assertIn("already registered by alice@vm1", str(cm.exception))
        self.assertEqual(self.last("project.registered")["by"], "alice@vm1")

    def test_name_and_id_validation(self):
        for bad in ("", " ", "-lead", "a~b", "x" * 41, "semi;colon", "/lead", "trail/"):
            with self.assertRaises(ApiError, msg=bad) as cm:
                self.mgr.register_project(bad, PID, "alice")
            self.assertEqual(cm.exception.code, 400)
        for bad in ("short", "bad id with spaces!", None):
            with self.assertRaises(ApiError):
                self.mgr.register_project("ok", bad, "alice")
        self.assertEqual(self.mgr.register_project("My Game 2.0", PID, "alice")["name"], "My Game 2.0")
        self.assertEqual(self.mgr.register_project("game/render", PID2, "alice")["name"], "game/render")

    def test_ad_hoc_label_clashing_with_registered_name_is_qualified(self):
        self.mgr.register_project("game", PID, "alice@vm1")
        r = self.acquire("Game", owner="bob@vm2")
        self.assertEqual(r["project"], "Game~bob")
        self.assertIn("registered project", r["note"])
        self.assertEqual(self.mgr.history[-1]["project"], "Game~bob")

    def test_id_from_dcm_json_registers_on_first_use(self):
        r = self.acquire("game", PID)
        self.assertEqual((r["project"], r["note"]), ("game", None))
        self.assertEqual(self.mgr.store.project(pid=PID)["owner"], "alice@vm1")
        r = self.acquire("game", PID, owner="bob@vm2")  # a teammate with the same .dcm.json
        self.assertEqual((r["project"], r["note"]), ("game", None))
        self.assertIsNotNone(self.mgr.store.project(pid=PID)["last_used"])

    def test_unknown_id_with_taken_name_is_refused(self):
        self.mgr.register_project("game", PID, "alice@vm1")
        with self.assertRaises(ApiError) as cm:
            self.acquire("game", PID2, owner="bob@vm2")
        self.assertEqual(cm.exception.code, 409)
        self.assertEqual(self.mgr.status()["queue"], [])

    def test_editing_dcm_json_renames_and_carries_history(self):
        self.acquire("game", PID)
        r = self.acquire("Game Two", PID)
        self.assertEqual(r["project"], "Game Two")
        self.assertIn("renamed", r["note"])
        self.assertEqual([h["project"] for h in self.mgr.query_sessions()], ["Game Two", "Game Two"])
        self.assertEqual(self.mgr.query_events(project="game"), [])
        self.assertEqual(self.last("project.renamed")["old"], "game")

    def test_rename_to_taken_name_keeps_registered_name(self):
        self.mgr.register_project("other", PID2, "bob")
        self.acquire("game", PID)
        r = self.acquire("other", PID)
        self.assertEqual(r["project"], "game")
        self.assertIn("not renamed", r["note"])

    def test_hold_labels_are_resolved_too(self):
        self.mgr.register_project("hands-on", PID, "alice")
        self.mgr.hold("bob", 15)
        self.assertEqual(self.mgr.lease["project"], "hands-on~bob")

    def test_delete_keeps_history(self):
        self.acquire("game", PID)
        self.mgr.delete_project("GAME", "admin")
        self.assertIsNone(self.mgr.store.project(pid=PID))
        self.assertEqual(self.mgr.query_sessions()[-1]["project"], "game")
        with self.assertRaises(ApiError) as cm:
            self.mgr.delete_project("game", "admin")
        self.assertEqual(cm.exception.code, 404)

    def test_project_list_with_stats(self):
        self.mgr.register_project("game", PID, "alice")
        self.mgr.register_project("idle", PID2, "bob")
        self.acquire("game", PID)
        self.acquire("game", PID)
        self.acquire("scratch")
        by_name = {p["name"]: p for p in self.mgr.store.projects()}
        self.assertEqual((by_name["game"]["sessions"], by_name["game"]["registered"]), (2, True))
        self.assertEqual((by_name["idle"]["sessions"], by_name["idle"]["last_session"]), (0, None))
        self.assertEqual((by_name["scratch"]["sessions"], by_name["scratch"]["registered"]), (1, False))

    def test_v1_database_is_upgraded(self):
        path = os.path.join(self.tmp, "v1.db")
        db = sqlite3.connect(path)
        db.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
                         "INSERT INTO meta VALUES ('schema_version', '1');")
        db.commit()
        db.close()
        st = srv.Store(path)
        try:
            self.assertEqual(st.meta("schema_version"), str(srv.SCHEMA_VERSION))
            st.add_project(PID, "game", "alice")
            self.assertEqual(st.project(name="GAME")["id"], PID)
        finally:
            st.close()


class ProjectHttpTests(HttpCase):
    def test_register_list_delete(self):
        code, p = self.post("/projects/register", {"name": "game", "id": PID, "owner": "alice"})
        self.assertEqual((code, p["name"]), (200, "game"))
        self.assertEqual(self.post("/projects/register", {"name": "game", "id": PID2})[0], 409)
        self.assertEqual(self.post("/projects/register", {"name": "a~b", "id": PID2})[0], 400)
        code, _, data = self.request("/projects", token=None)
        self.assertEqual([p["name"] for p in json.loads(data)["projects"]], ["game"])
        self.assertEqual(self.post("/projects/delete", {"name": "game"})[0], 200)
        self.assertEqual(self.post("/projects/delete", {"name": "game"})[0], 404)

    def test_acquire_reports_effective_project(self):
        self.post("/projects/register", {"name": "game", "id": PID, "owner": "alice"})
        _, r = self.post("/acquire", {"owner": "bob@vm", "project": "game"})
        self.assertEqual(r["project"], "game~bob")
        self.assertTrue(r["note"])


class ProjectClientTests(HttpCase):
    dcm = test_client.ClientTests.dcm

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, ".git"))
        os.makedirs(os.path.join(self.repo, "src", "deep"))

    def dcm_in(self, cwd, *args):
        old = os.getcwd()
        os.chdir(cwd)
        try:
            return self.dcm(*args)
        finally:
            os.chdir(old)

    def test_init_writes_dcm_json_at_repo_root_and_run_uses_it(self):
        r = self.dcm_in(os.path.join(self.repo, "src"), "project", "init", "Space Game", "-d", "demo")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.repo, ".dcm.json")) as f:
            data = json.load(f)
        self.assertEqual(data["project"], "Space Game")
        self.assertEqual(self.mgr.store.project(pid=data["id"])["description"], "demo")
        r = self.dcm_in(os.path.join(self.repo, "src", "deep"), "run", "--", sys.executable, "-c", "pass")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.mgr.history[-1]["project"], "Space Game")
        # -p is an ad-hoc label: it clashes with the registered name, so it is qualified
        r = self.dcm_in(self.repo, "run", "-p", "space game", "--", sys.executable, "-c", "pass")
        self.assertIn("registered project", r.stderr)
        self.assertTrue(self.mgr.history[-1]["project"].startswith("space game~"), self.mgr.history[-1])

    def test_init_again_renames_and_keeps_id(self):
        self.dcm_in(self.repo, "project", "init", "first")
        with open(os.path.join(self.repo, ".dcm.json")) as f:
            pid = json.load(f)["id"]
        r = self.dcm_in(self.repo, "project", "init", "second")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.repo, ".dcm.json")) as f:
            self.assertEqual(json.load(f), {"project": "second", "id": pid})
        self.assertEqual(self.mgr.store.project(pid=pid)["name"], "second")

    def test_init_with_taken_name_fails_and_writes_nothing(self):
        self.mgr.register_project("taken", PID, "alice")
        r = self.dcm_in(self.repo, "project", "init", "taken")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already registered", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".dcm.json")))

    def test_list_and_forget(self):
        self.mgr.register_project("game", PID, "alice")
        r = self.dcm("project", "list")
        self.assertIn("game", r.stdout)
        self.assertIn("registered by alice", r.stdout)
        self.assertEqual(self.dcm("project", "forget", "game").returncode, 0)
        self.assertIsNone(self.mgr.store.project(pid=PID))

    def test_bad_dcm_json_is_reported(self):
        with open(os.path.join(self.repo, ".dcm.json"), "w") as f:
            f.write("{broken")
        r = self.dcm_in(self.repo, "run", "--", sys.executable, "-c", "pass")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cannot read", r.stderr)


if __name__ == "__main__":
    unittest.main()
