"""The HTTP API, web GUI and capture streams."""
import html
import itertools
import json
import os
import re
import secrets
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler
from .common import ApiError, HERE, log
from .calibrate import elf_path
from .guide import CLIENT_DEFAULTS_LINE, render_guide

class Handler(BaseHTTPRequestHandler):
    mgr = None  # set in main()

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, code, body, ctype="application/json"):
        data = (body if isinstance(body, bytes) else
                body.encode() if isinstance(body, str) else json.dumps(body).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _base(self):
        return f"http://{self.headers.get('Host') or '%s:%d' % self.server.server_address[:2]}"

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/status":
            return self._send(200, self.mgr.status())
        if path == "/events":
            return self._send_events()
        if path == "/sessions":
            return self._send_sessions()
        if path == "/console":
            return self._send_console()
        if path == "/calibrate":
            return self._send(200, self.mgr.calibrator.status())
        if path == "/calibrate/calib.elf":
            return self._send_file(elf_path(self.mgr.cfg), "application/octet-stream")
        if path == "/capture/settings":
            try:
                return self._send(200, self.mgr.capture_settings())
            except ApiError as e:
                return self._send(e.code, {"error": str(e)})
        m = re.fullmatch(r"/recordings/([0-9a-f]{12})\.mp4", path)
        if m:
            return self._send_file(self.mgr.recorder.path(m.group(1)), "video/mp4")
        m = re.fullmatch(r"/sessions/([0-9a-f]{12})/frame\.jpg", path)
        if m:
            return self._send_frame(m.group(1))
        if path == "/projects":
            return self._send(200, {"projects": self.mgr.store.projects()})
        if path == "/dcm.py":
            return self._send_client()
        if path in ("/capture.jpg", "/screenshot.jpg"):
            try:
                return self._send(200, self.mgr.capture.snapshot(), "image/jpeg")
            except ApiError as e:
                return self._send(e.code, {"error": str(e)})
        if path == "/capture.mjpg":
            return self._send_stream()
        if path == "/capture.mp3":
            return self._send_feed(self.mgr.audio, "audio/mpeg", True)
        if path == "/capture.mkv":
            return self._send_av()
        if path == "/capture.m3u":
            return self._send_playlist()
        md = "text/markdown; charset=utf-8"
        if path in ("/guide", "/guide.md", "/llms.txt", "/AGENTS.md"):
            return self._send(200, render_guide(self.mgr, self._base()), md)
        m = re.fullmatch(r"/session/([0-9a-f]{12})", path)
        if m:
            return self._send_session_page()
        if path != "/":
            return self._send(404, {"error": "not found; try / or /guide"})
        # Browsers get the GUI; curl and agent fetch tools get the Markdown guide.
        if "text/html" not in self.headers.get("Accept", ""):
            return self._send(200, render_guide(self.mgr, self._base()), md)
        self._send_gui()

    def _send_events(self):
        """GET /events?kind=lease&user=alice&project=game&level=warning&q=text&since=<unix>&limit=200"""
        qs = urllib.parse.parse_qs(self.path.partition("?")[2])
        args = {k: v[-1] for k, v in qs.items()
                if k in ("kind", "user", "project", "level", "console", "q", "since", "until", "limit")}
        try:
            events = self.mgr.query_events(**args)
        except ValueError as e:
            return self._send(400, {"error": f"bad filter: {e}"})
        self._send(200, {"events": events})

    def _send_file(self, path, ctype):
        """A file with HTTP Range support, so browsers can seek in recordings."""
        try:
            f = open(path, "rb")
        except OSError:
            return self._send(404, {"error": "no such recording (never made, or deleted to stay within "
                                             "recording_max_gb)"})
        with f:
            size = os.fstat(f.fileno()).st_size
            start, end = 0, size - 1
            rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
            if rng and (rng.group(1) or rng.group(2)):
                if rng.group(1):
                    start = int(rng.group(1))
                    end = min(int(rng.group(2)), size - 1) if rng.group(2) else size - 1
                else:  # the last N bytes
                    start = max(0, size - int(rng.group(2)))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(left, 1 << 16))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except OSError:
                pass  # viewer went away or seeked elsewhere

    def _send_frame(self, uid):
        """GET /sessions/<id>/frame.jpg?t=<unix time>: the recorded screen when a console line was printed."""
        qs = urllib.parse.parse_qs(self.path.partition("?")[2])
        try:
            t = float(qs["t"][-1])
            jpg = self.mgr.recorder.frame(uid, t)
        except (KeyError, ValueError):
            return self._send(400, {"error": "t (unix time) is required"})
        except ApiError as e:
            return self._send(e.code, {"error": str(e)})
        self._send(200, jpg, "image/jpeg")

    def _send_console(self):
        """GET /console?session=<id>&user=<owner>&project=<p>&after=<seq>&limit=1000&wait=<s>&tail=1
        Without session: the live session (if it matches user/project), else the latest one with output."""
        qs = urllib.parse.parse_qs(self.path.partition("?")[2])
        args = {k: v[-1] for k, v in qs.items()
                if k in ("session", "user", "project", "after", "limit", "wait", "tail")}
        if "tail" in args:
            args["tail"] = args["tail"] not in ("", "0", "false")
        try:
            r = self.mgr.console_read(**args)
        except ApiError as e:
            return self._send(e.code, {"error": str(e)})
        except ValueError as e:
            return self._send(400, {"error": f"bad parameter: {e}"})
        self._send(200, r)

    def _send_sessions(self):
        """GET /sessions?user=alice&project=game&outcome=vacated&since=<unix>&limit=50"""
        qs = urllib.parse.parse_qs(self.path.partition("?")[2])
        args = {k: v[-1] for k, v in qs.items()
                if k in ("user", "project", "console", "outcome", "since", "until", "limit", "uid")}
        try:
            sessions = self.mgr.query_sessions(**args)
        except ValueError as e:
            return self._send(400, {"error": f"bad filter: {e}"})
        self._send(200, {"sessions": sessions})

    def _send_gui(self):
        try:
            with open(os.path.join(HERE, "web", "index.html"), encoding="utf-8") as f:
                page = f.read()
        except OSError:
            return self._send(200, render_guide(self.mgr, self._base()), "text/plain; charset=utf-8")
        cfg = self.mgr.cfg
        controls = cfg["web_controls"]
        # The state goes inside <script type="application/json">; escaping "<" keeps owner
        # names from closing the tag. The guide is rendered server-side so agents that fetch
        # with Accept: text/html still see it without running JavaScript.
        state = json.dumps(self.mgr.status()).replace("<", "\\u003c")
        page = (page.replace("<!--DCMGR:STATE-->", state)
                    .replace("<!--DCMGR:GUIDE-->", html.escape(render_guide(self.mgr, self._base())))
                    .replace("<!--DCMGR:TOKEN-->", html.escape(cfg["api_token"] if controls else ""))
                    .replace("<!--DCMGR:CONTROLS-->", "1" if controls else "0"))
        self._send(200, page, "text/html; charset=utf-8")

    def _send_session_page(self):
        """/session/<id>: one session's console output next to its recording (web/session.html)."""
        try:
            with open(os.path.join(HERE, "web", "session.html"), encoding="utf-8") as f:
                page = f.read()
        except OSError:
            return self._send(404, {"error": "web/session.html is not installed"})
        cfg = self.mgr.cfg
        controls = cfg["web_controls"]
        page = (page.replace("<!--DCMGR:TOKEN-->", html.escape(cfg["api_token"] if controls else ""))
                    .replace("<!--DCMGR:CONTROLS-->", "1" if controls else "0"))
        self._send(200, page, "text/html; charset=utf-8")

    def _send_feed(self, feed, ctype, every, wrap=lambda d: d):
        try:
            items = feed.follow(every)
            first = next(items)  # fail with a proper HTTP error if the device gives nothing
        except ApiError as e:
            return self._send(e.code, {"error": str(e)})
        except StopIteration:
            return self._send(503, {"error": f"no data from the {feed.what} device: {feed.error or 'timed out'}"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            for batch in itertools.chain([first], items):
                self.wfile.write(b"".join(wrap(d) for d in batch))
                self.wfile.flush()
        except OSError:
            pass  # viewer went away
        finally:
            items.close()

    def _send_stream(self):
        self._send_feed(self.mgr.capture, "multipart/x-mixed-replace; boundary=frame", False,
                        lambda jpg: b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n%s\r\n"
                                    % (len(jpg), jpg))

    def _send_av(self):
        """Picture + sound in one Matroska stream for VLC. A per-viewer ffmpeg remuxes this server's
        own MJPEG and MP3 feeds (no re-encoding), so the devices stay shared."""
        m = self.mgr
        if not m.capture.enabled:
            return self._send(404, {"error": "no capture device configured (capture_device)"})
        host = m.cfg["listen"] if m.cfg["listen"] not in ("0.0.0.0", "") else "127.0.0.1"
        base = f"http://{host}:{m.cfg['port']}"
        clock = ["-use_wallclock_as_timestamps", "1"]
        cmd = [m.cfg["capture_ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostdin",
               *clock, "-f", "mpjpeg", "-i", base + "/capture.mjpg"]
        if m.audio.enabled:
            cmd += [*clock, "-f", "mp3", "-i", base + "/capture.mp3", "-map", "0:v", "-map", "1:a"]
        cmd += ["-c", "copy", "-flush_packets", "1", "-f", "matroska", "-live", "1",
                "-cluster_time_limit", "500", "pipe:1"]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
        except OSError as e:
            return self._send(503, {"error": f"cannot run ffmpeg: {e}"})
        self.send_response(200)
        self.send_header("Content-Type", "video/x-matroska")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                chunk = proc.stdout.read1(1 << 16)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except OSError:
            pass  # viewer went away
        finally:
            proc.kill()
            proc.wait()

    def _send_playlist(self):
        # opening an .m3u launches VLC (or whatever plays playlists); low caching keeps it near-live
        body = (f"#EXTM3U\n#EXTINF:-1,Dreamcast (dcmgr)\n#EXTVLCOPT:network-caching=300\n"
                f"{self._base()}/capture.mkv\n")
        self.send_response(200)
        self.send_header("Content-Type", "audio/x-mpegurl")
        self.send_header("Content-Disposition", 'attachment; filename="dreamcast.m3u"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    def _send_client(self):
        """Serve dcm.py with its defaults pointed at this server (and the token, if published)."""
        try:
            with open(os.path.join(HERE, "dcm.py")) as f:
                src = f.read()
        except OSError:
            return self._send(404, {"error": "dcm.py is not installed next to the server"})
        token = self.mgr.cfg["api_token"] if self.mgr.cfg["guide_includes_token"] else ""
        line = (f'    cfg = {{"url": {json.dumps(self._base())}, "token": {json.dumps(token)}, '
                f'"dc_tool": "dc-tool-ip"}}\n')
        if CLIENT_DEFAULTS_LINE not in src:
            log.warning("dcm.py defaults line not found; serving it unmodified")
        self._send(200, src.replace(CLIENT_DEFAULTS_LINE, line), "text/x-python; charset=utf-8")

    def do_POST(self):
        token = self.mgr.cfg["api_token"]
        if token and not secrets.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}"):
            return self._send(401, {"error": "bad or missing API token"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            b = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "invalid JSON"})
        m, by = self.mgr, str(b.get("owner") or self.client_address[0])[:80]
        routes = {
            "/acquire": lambda: m.acquire(by, b.get("project", "?"), b.get("ticket"), b.get("wait", 0),
                                          b.get("project_id")),
            "/projects/register": lambda: m.register_project(b.get("name"), b.get("id"), by, b.get("description")),
            "/projects/delete": lambda: m.delete_project(b.get("name"), by),
            "/heartbeat": lambda: m.heartbeat(b.get("token")),
            "/console": lambda: m.console_append(b.get("token"), b.get("lines"), b.get("sent")),
            "/sessions/pin": lambda: m.pin(str(b.get("uid")), bool(b.get("pinned", True)), by),
            "/capture/adjust": lambda: m.capture_adjust(bool(b.get("on", True)), by),
            "/calibrate/run": lambda: m.calibrator.start(by),
            "/calibrate/analyze": lambda: m.calibrator.analyze_session(b.get("session")),
            "/calibrate/apply": lambda: m.set_capture_delay(b.get("delay_ms"), by, bool(b.get("reset"))),
            "/capture/crop": lambda: m.capture_crop(b.get("crop"), by, bool(b.get("reset"))),
            "/prepare": lambda: m.prepare(b.get("token"), b.get("reset", True)),
            "/release": lambda: m.release(b.get("token"), b.get("keep", False), b.get("force", False), by,
                                          b.get("drain", True), b.get("reason")),
            "/reset": lambda: m.reset(b.get("token"), b.get("force", False), b.get("reason", "manual"), by,
                                      wait=not b.get("async", False)),
            # GUI actions
            "/vacate": lambda: m.release(None, b.get("keep", False), True, by, b.get("drain", True),
                                         b.get("reason")),
            "/queue/evict": lambda: m.evict(b.get("id"), by),
            "/queue/move": lambda: m.move(b.get("id"), b.get("to", 0), by),
            "/hold": lambda: m.hold(by, b.get("minutes", 30), b.get("project")),
            "/hold/extend": lambda: m.extend(by, b.get("minutes", 15)),
            "/nudge": lambda: m.nudge(by, b.get("message")),
            "/maintenance": lambda: m.set_maintenance(bool(b.get("on")), by, b.get("note", ""),
                                                      b.get("mode") or "now"),
            "/power": lambda: m.power(bool(b.get("on")), by, b.get("force", False)),
        }
        if self.path not in routes:
            return self._send(404, {"error": "not found"})
        try:
            self._send(200, routes[self.path]())
        except ApiError as e:
            self._send(e.code, {"error": str(e)})
        except (ValueError, TypeError) as e:
            self._send(400, {"error": f"bad request: {e}"})
        except Exception as e:
            log.exception("handler error")
            self._send(500, {"error": str(e)})
