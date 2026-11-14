"""HTTP API: auth, routing, error mapping, the GUI page, the agent guide and the served client."""
import json
import threading
import unittest
import urllib.error
import urllib.request

from rig import RigCase, srv


class HttpCase(RigCase):
    config = {"api_token": "secret"}

    def setUp(self):
        super().setUp()
        handler = type("TestHandler", (srv.Handler,), {"mgr": self.mgr})
        self.http = srv.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        super().tearDown()

    def request(self, path, body=None, token="secret", accept=None, raw=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if accept:
            headers["Accept"] = accept
        data = raw if raw is not None else None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, headers=headers,
                                     method="GET" if data is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers.get("Content-Type", ""), e.read()

    def post(self, path, body=None, **kw):
        code, _, data = self.request(path, body if body is not None else {}, **kw)
        return code, json.loads(data)


class ApiTests(HttpCase):
    def test_post_needs_the_token(self):
        self.assertEqual(self.post("/acquire", {"owner": "a"}, token=None)[0], 401)
        self.assertEqual(self.post("/acquire", {"owner": "a"}, token="wrong")[0], 401)
        code, r = self.post("/acquire", {"owner": "alice", "project": "p"})
        self.assertEqual(code, 200)
        self.assertTrue(r["granted"])

    def test_lock_cycle_over_http(self):
        _, r = self.post("/acquire", {"owner": "alice", "project": "p"})
        code, p = self.post("/prepare", {"token": r["token"]})
        self.assertEqual((code, p["ip"]), (200, self.ips[0]))
        self.assertEqual(self.post("/heartbeat", {"token": r["token"]})[0], 200)
        self.assertEqual(self.post("/release", {"token": r["token"]})[1], {"released": True})

    def test_errors_map_to_status_codes(self):
        code, r = self.post("/heartbeat", {"token": "bogus"})
        self.assertEqual(code, 409)
        self.assertIn("do not hold", r["error"])
        self.assertEqual(self.post("/nope")[0], 404)
        self.assertEqual(self.post("/queue/evict", {"id": "x"})[0], 404)
        self.assertEqual(self.post("/hold", {"minutes": "lots"})[0], 400)
        self.assertEqual(self.request("/acquire", raw=b"{oops")[0], 400)

    def test_gui_vacate_and_queue_actions(self):
        _, r = self.post("/acquire", {"owner": "alice", "project": "p"})
        self.post("/acquire", {"owner": "bob", "project": "p", "wait": 0.05})
        qid = self.mgr.status()["queue"][0]["id"]
        self.assertEqual(self.post("/queue/evict", {"id": qid, "owner": "admin"})[0], 200)
        self.assertEqual(self.post("/vacate", {"owner": "admin", "drain": False})[1], {"released": True})
        self.assertEqual(self.post("/heartbeat", {"token": r["token"]})[0], 409)

    def test_status_is_public_and_versioned(self):
        code, ctype, data = self.request("/status", token=None)
        self.assertEqual(code, 200)
        st = json.loads(data)
        self.assertEqual(st["version"], srv.__version__)
        self.assertEqual(st["dc"]["ip"], self.ips[0])

    def test_capture_without_device(self):
        self.assertEqual(self.request("/capture.jpg")[0], 404)


class PageTests(HttpCase):
    def test_browsers_get_gui_and_agents_get_guide(self):
        code, ctype, page = self.request("/", accept="text/html")
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        self.assertNotIn(b"<!--DCMGR:STATE-->", page)
        code, ctype, guide = self.request("/", accept="*/*")
        self.assertIn("text/markdown", ctype)
        for path in ("/guide", "/llms.txt", "/AGENTS.md"):
            self.assertEqual(self.request(path)[2], guide)

    def test_owner_names_cannot_break_out_of_the_page(self):
        self.post("/acquire", {"owner": "</script><img src=x onerror=alert(1)>", "project": "p"})
        _, _, page = self.request("/", accept="text/html")
        self.assertNotIn(b"</script><img", page)

    def test_read_only_page_hides_token(self):
        self.cfg["web_controls"] = False
        _, _, page = self.request("/", accept="text/html")
        self.assertNotIn(b"secret", page)

    def test_served_client_points_at_this_server(self):
        code, _, src = self.request("/dcm.py", token=None)
        self.assertEqual(code, 200)
        self.assertIn(f'"url": "{self.base}", "token": ""'.encode(), src)
        self.cfg["guide_includes_token"] = True
        _, _, src = self.request("/dcm.py", token=None)
        self.assertIn(b'"token": "secret"', src)

    def test_example_config_is_current(self):
        # every key in the shipped example must be one the server knows (it had drifted before 1.0)
        with open(srv.os.path.join(srv.HERE, "dcmgr.example.json")) as f:
            example = json.load(f)
        self.assertEqual(sorted(set(example) - set(srv.DEFAULTS)), [])

    def test_client_defaults_line_matches(self):
        # _send_client rewrites this exact line; if dcm.py changes it, served clients silently break
        with open(srv.os.path.join(srv.HERE, "dcm.py")) as f:
            self.assertIn(srv.CLIENT_DEFAULTS_LINE, f.read())


if __name__ == "__main__":
    unittest.main()
