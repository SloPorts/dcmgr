#!/usr/bin/env python3
"""dcm: share one dcload-ip Dreamcast between machines via dcmgr-server.

  dcm status                        who has the DC, where it is, recent events
  dcm load game.elf [-- ARGS]       lock, locate DC, dc-tool-ip -x game.elf ARGS, unlock
  dcm run -- CMD ...                same for any command; {ip} and $DC_IP are substituted
  dcm shell                         hold the lock for an interactive shell ($DC_IP set)
  dcm reset                         power-cycle the DC via Home Assistant
  dcm ip                            print the DC's last known IP
  dcm guide                         live setup/usage guide for this rig (for agents)
  dcm screenshot [shot.jpg]         save the DC's screen from the capture device (no lock needed)
  dcm console [-f]                  console output of your last run (-f: follow a live one)
  dcm unlock --force                break someone else's lock
  dcm project init [NAME]           give this repo a unique project name (writes .dcm.json)
  dcm project list                  projects known to the server

Config: $DCMGR_URL, $DCMGR_TOKEN, $DC_TOOL, or ~/.config/dcmgr/client.json with
{"url": ..., "token": ..., "dc_tool": ...}. Stdlib only, Python 3.8+.
"""
import argparse
import getpass
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

__version__ = "1.0.0"

OWNER = f"{getpass.getuser()}@{socket.gethostname()}"
PROJECT_FILE = ".dcm.json"


def load_config():
    cfg = {"url": "http://localhost:8731", "token": "", "dc_tool": "dc-tool-ip"}
    path = os.environ.get("DCMGR_CLIENT_CONFIG",
                          os.path.join(os.path.expanduser("~"), ".config", "dcmgr", "client.json"))
    if os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    for key, env in (("url", "DCMGR_URL"), ("token", "DCMGR_TOKEN"), ("dc_tool", "DC_TOOL")):
        cfg[key] = os.environ.get(env, cfg[key])
    return cfg


def say(msg):
    print(f"[dcm] {msg}", file=sys.stderr, flush=True)


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


class Api:
    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token

    def call(self, path, body=None, timeout=30, raw=False):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.url + path, data=data, headers=headers,
                                     method="GET" if data is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if raw == "bytes":
                    return r.read()
                return r.read().decode() if raw else json.load(r)
        except urllib.error.HTTPError as e:
            try:
                msg = json.load(e).get("error", e.reason)
            except ValueError:
                msg = e.reason
            raise ApiError(e.code, msg) from None
        except (urllib.error.URLError, OSError) as e:
            raise ApiError(0, f"cannot reach dcmgr at {self.url}: {getattr(e, 'reason', e)}") from None


def find_upwards(name, start=None):
    """Path of `name` in start (default cwd) or the nearest parent directory that has it, else None."""
    d = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.exists(os.path.join(d, name)):
            return os.path.join(d, name)
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def read_project_file():
    """(path, data) of the nearest .dcm.json, or (None, {})."""
    path = find_upwards(PROJECT_FILE)
    if not path:
        return None, {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as e:
        sys.exit(f"[dcm] cannot read {path}: {e}")
    return path, data


def project_identity(explicit=None):
    """(name, id) to lock under: -p NAME as an ad-hoc label, else the repo's .dcm.json, else the
    directory name. The server keeps registered names unique; ad-hoc labels that clash with one
    are run as NAME~user."""
    if explicit:
        return explicit, None
    _, data = read_project_file()
    if data.get("project"):
        return data["project"], data.get("id")
    return os.path.basename(os.getcwd()), None


def describe_holder(h):
    if not h:
        return "nobody"
    return f"{h['owner']} ({h['project']}) for {(time.time() - h['acquired']) / 60:.0f} min"


class Lease:
    """Holds the DC lease for the duration of a `with` block, heartbeating in the background.

    If $DCMGR_LEASE is set (we're inside `dcm shell` or `dcm run`), that lease is reused
    instead and the parent stays responsible for heartbeats and release.
    """

    def __init__(self, api, project, wait=True, keep=False):
        self.api, self.wait, self.keep = api, wait, keep
        self.project, self.project_id = project if isinstance(project, tuple) else (project, None)
        self.token, self.owned = os.environ.get("DCMGR_LEASE"), False
        self.lost, self._stop = threading.Event(), threading.Event()

    def __enter__(self):
        if self.token:
            self.api.call("/heartbeat", {"token": self.token})
            return self
        ticket, last_pos = None, None
        while True:
            # short first poll so the queue message appears promptly, then long-poll
            wait = (20 if ticket else 1) if self.wait else 0
            r = self.api.call("/acquire", {"owner": OWNER, "project": self.project, "project_id": self.project_id,
                                           "ticket": ticket, "wait": wait}, timeout=45)
            if r.get("note") and not ticket:
                say(r["note"])
            if r["granted"]:
                break
            if not self.wait:
                sys.exit(f"[dcm] DC is busy: held by {describe_holder(r['holder'])}")
            ticket = r["ticket"]
            maint = r.get("maintenance")
            why = (f"paused for maintenance by {maint['by']}" + (f" ({maint['note']})" if maint["note"] else "")
                   if maint else f"held by {describe_holder(r['holder'])}" if r["holder"]
                   else "finishing a forced release" if r.get("draining") else "busy")
            if (r["position"], why) != last_pos:
                say(f"waiting for DC: {why}, you are #{r['position']} in queue (Ctrl-C to give up)")
                last_pos = (r["position"], why)
        self.token, self.owned, self.ttl = r["token"], True, r["ttl"]
        threading.Thread(target=self._heartbeat, daemon=True).start()
        say("lock acquired")
        return self

    def _heartbeat(self):
        while not self._stop.wait(self.ttl / 3):
            try:
                r = self.api.call("/heartbeat", {"token": self.token})
            except ApiError as e:
                if e.code == 409:
                    say(f"LOST THE DC LEASE: {e} - stopping")
                    self.lost.set()
                    return
                say(f"heartbeat failed, retrying: {e}")
                continue
            for n in r.get("notices", []):
                say(f"message from {n['by']}: {n['msg']}")

    def __exit__(self, *exc):
        self._stop.set()
        if self.owned and not self.lost.is_set():
            try:
                self.api.call("/release", {"token": self.token, "keep": self.keep})
                say("lock released" + (" (DC left running)" if self.keep else ""))
            except ApiError as e:
                say(f"release failed ({e}); lease will expire on its own")

    def prepare(self, allow_reset=True):
        say("locating DC...")
        try:
            r = self.api.call("/prepare", {"token": self.token, "reset": allow_reset}, timeout=300)
        except ApiError as e:
            sys.exit(f"[dcm] {e}")
        say(f"DC ready at {r['ip']} ({r['version']})" if r["ip"] else "DC ready (the server doesn't check this loader)")
        return r["ip"]

    def reset(self, reason):
        say(f"power-cycling DC ({reason})...")
        r = self.api.call("/reset", {"token": self.token, "reason": reason, "owner": OWNER}, timeout=300)
        say(f"DC back at {r['ip']}")
        return r["ip"]


class ConsoleUploader:
    """Sends what the loader prints to dcmgr in the background, line by line with the time each line
    arrived, so it can be read back (dcm console, the web GUI) and lined up with the screen recording.
    Never blocks the output: if the server is unreachable, lines queue up (and the oldest are dropped)."""

    MAX_PENDING = 20000

    def __init__(self, api, token):
        self.api, self.token = api, token
        self.pending, self.partial, self.partial_t = [], b"", None
        self.sent = self.dropped = 0
        self.dead = None  # why uploading stopped for good
        self.lock, self.wake, self.stopping = threading.Lock(), threading.Event(), False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def feed(self, chunk):
        now = time.time()
        with self.lock:
            if self.partial_t is None:
                self.partial_t = now  # a line's time is when its first bytes arrived
            *full, self.partial = (self.partial + chunk).split(b"\n")
            for raw in full:
                self._add(self.partial_t, raw)
                self.partial_t = now
            if not self.partial:
                self.partial_t = None
        if len(self.pending) >= 500:
            self.wake.set()

    def _add(self, t, raw):  # caller holds self.lock
        self.pending.append([t, raw.decode("utf-8", "replace").rstrip("\r")])
        if len(self.pending) > self.MAX_PENDING:
            drop = len(self.pending) - self.MAX_PENDING
            del self.pending[:drop]
            self.dropped += drop

    def _send(self):
        with self.lock:
            # a line without its newline yet goes out once it has waited a second
            if self.partial and (self.stopping or time.time() - self.partial_t > 1.0):
                self._add(self.partial_t, self.partial)
                self.partial, self.partial_t = b"", None
            batch = self.pending[:2000]
        if not batch or self.dead:
            return True
        try:
            self.api.call("/console", {"token": self.token, "lines": batch, "sent": time.time()}, timeout=15)
        except ApiError as e:
            if e.code in (400, 404, 409):  # lease gone, or a server without console support
                self.dead = str(e)
                return True
            return False  # unreachable: keep the lines and retry
        with self.lock:
            del self.pending[:len(batch)]
            self.sent += len(batch)
        return True

    def _run(self):
        delay = 0.5
        while not self.stopping:
            self.wake.wait(delay)
            self.wake.clear()
            delay = 0.5 if self._send() else min(delay * 2, 10)

    def close(self, timeout=10.0):
        """Send everything still queued (called before the lock is released)."""
        self.stopping = True
        self.wake.set()
        self.thread.join(2)
        deadline = time.time() + timeout
        while (self.pending or self.partial) and not self.dead and time.time() < deadline:
            if not self._send():
                time.sleep(0.5)
        lost = self.dropped + len(self.pending)
        if self.dead and self.dead.startswith("not found"):
            return  # server predates console capture; say nothing
        if self.dead:
            say(f"console upload stopped: {self.dead}")
        elif lost:
            say(f"console upload: {lost} lines could not be sent to dcmgr")


def run_child(cmd, lease, ip, idle_timeout=0, timeout=0, log_path=None, console=None):
    """Run cmd against the DC. Returns (exit code, reason it was stopped or None).
    console: a ConsoleUploader that gets a copy of the output."""
    cmd = [a.replace("{ip}", ip) for a in cmd]
    env = dict(os.environ, DC_IP=ip, DCMGR_LEASE=lease.token)
    pipe = bool(idle_timeout or log_path or console)
    if pipe:  # keep the idle watchdog honest: stop children block-buffering into our pipe
        env["PYTHONUNBUFFERED"] = "1"
        if os.name != "nt" and shutil.which("stdbuf"):
            cmd = ["stdbuf", "-o0", "-e0"] + cmd
    try:
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE if pipe else None,
                                stderr=subprocess.STDOUT if pipe else None)
    except OSError as e:
        sys.exit(f"[dcm] cannot start {cmd[0]}: {e}")
    last_output = [time.monotonic()]

    def pump():
        logf = open(log_path, "ab") if log_path else None
        try:
            while True:
                chunk = os.read(proc.stdout.fileno(), 4096)
                if not chunk:
                    break
                last_output[0] = time.monotonic()
                sys.stdout.buffer.write(chunk)
                sys.stdout.flush()
                if console:
                    console.feed(chunk)
                if logf:
                    logf.write(chunk)
                    logf.flush()
        finally:
            if logf:
                logf.close()

    pumper = threading.Thread(target=pump, daemon=True) if pipe else None
    if pumper:
        pumper.start()
    start, reason = time.monotonic(), None
    try:
        while proc.poll() is None:
            time.sleep(0.2)
            now = time.monotonic()
            if lease.lost.is_set():
                reason = "lease lost"
            elif timeout and now - start > timeout:
                reason = f"timed out after {timeout}s"
            elif idle_timeout and now - last_output[0] > idle_timeout:
                reason = f"no output for {idle_timeout}s - DC presumed frozen"
            if reason:
                break
    except KeyboardInterrupt:
        reason = "interrupted"
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    if pumper:
        pumper.join(2)
    if console:
        console.close()
    return proc.returncode, reason


def cmd_run(args, cfg, cmd):
    api = Api(cfg["url"], cfg["token"])
    with Lease(api, project_identity(args.project), wait=not args.no_wait, keep=args.keep) as lease:
        ip = lease.reset("requested --reset-first") if args.reset_first else lease.prepare(not args.no_reset)
        console = None if args.no_console else ConsoleUploader(api, lease.token)
        code, reason = run_child(cmd, lease, ip, args.idle_timeout, args.timeout, args.log, console)
        if console and console.sent:
            say(f"console output saved ({console.sent} lines); read it back with: dcm console")
        if reason:
            say(f"stopped: {reason}")
            if "frozen" in reason and not args.keep:
                try:
                    lease.reset("watchdog: " + reason)
                except ApiError as e:
                    say(f"reset failed: {e}")
            code = 130 if reason == "interrupted" else 124
    return code


def cmd_load(args, cfg):
    return cmd_run(args, cfg, [cfg["dc_tool"], "-t", "{ip}", "-x", args.elf] + strip_dashes(args.extra))


def cmd_shell(args, cfg):
    api = Api(cfg["url"], cfg["token"])
    candidates = [os.environ.get("SHELL"), os.environ.get("COMSPEC"), "/bin/sh"]
    shell = next((shutil.which(c) for c in candidates if c and shutil.which(c)), None)
    if not shell:
        sys.exit("[dcm] no usable shell found ($SHELL / $COMSPEC)")
    with Lease(api, project_identity(args.project), wait=not args.no_wait, keep=args.keep) as lease:
        ip = lease.prepare(not args.no_reset)
        say(f"holding the DC; $DC_IP={ip}. Nested dcm commands reuse this lock. Exit the shell to release.")
        env = dict(os.environ, DC_IP=ip, DCMGR_LEASE=lease.token)
        proc = subprocess.Popen([shell], env=env)
        while proc.poll() is None:
            try:
                proc.wait(1)
            except subprocess.TimeoutExpired:
                if lease.lost.is_set():
                    say("the lock is gone - another user may now be using the DC")
            except KeyboardInterrupt:
                pass  # the shell handles its own Ctrl-C
    return proc.returncode


def cmd_status(args, cfg):
    st = Api(cfg["url"], cfg["token"]).call("/status")
    if args.json:
        print(json.dumps(st, indent=2))
        return 0
    dc = st["dc"]
    seen = time.strftime("%H:%M:%S", time.localtime(dc["last_seen"])) if dc["last_seen"] else "never"
    print(f"DC:     {dc['state']} at {dc['ip']} ({dc['version'] or 'version unknown'}), last seen {seen}")
    print(f"Holder: {describe_holder(st['lease'])}")
    if st["queue"]:
        print("Queue:  " + ", ".join(f"{q['owner']} ({q['project']})" for q in st["queue"]))
    print(f"Resets: {st['resets_last_hour']} in the last hour"
          + ("" if st.get("power_enabled", st.get("ha_enabled")) else " (no power control configured)"))
    for e in st["events"][-args.events:]:
        print(f"  {time.strftime('%m-%d %H:%M:%S', time.localtime(e['t']))}  {e['msg']}")
    return 0


def cmd_guide(args, cfg):
    print(Api(cfg["url"], cfg["token"]).call("/guide", raw=True))
    return 0


def cmd_ip(args, cfg):
    if os.environ.get("DC_IP"):
        print(os.environ["DC_IP"])
    else:
        print(Api(cfg["url"], cfg["token"]).call("/status")["dc"]["ip"])
    return 0


def cmd_reset(args, cfg):
    api = Api(cfg["url"], cfg["token"])
    r = api.call("/reset", {"token": os.environ.get("DCMGR_LEASE"), "force": args.force,
                            "reason": "manual", "owner": OWNER}, timeout=300)
    say(f"DC back at {r['ip']} ({r['version']})")
    return 0


def cmd_screenshot(args, cfg):
    jpg = Api(cfg["url"], cfg["token"]).call("/capture.jpg", timeout=20, raw="bytes")
    with open(args.out, "wb") as f:
        f.write(jpg)
    say(f"saved {args.out} ({len(jpg) // 1024} KB)")
    return 0


def cmd_console(args, cfg):
    api = Api(cfg["url"], cfg["token"])
    q = {}
    if args.session:
        q["session"] = args.session
    else:
        if not args.any:
            q["user"] = OWNER
        if args.project:
            q["project"] = args.project
    if args.lines:
        q.update(tail=1, limit=args.lines)
    else:
        q["limit"] = 10000
    try:
        r = api.call("/console?" + urllib.parse.urlencode(q))
    except ApiError as e:
        sys.exit(f"[dcm] {e}" + ("" if args.session or args.any else " (yours; --any for anyone's)"))
    info = r["session"]
    if not args.json:
        start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(info["start"]))
        state = "live" if r["live"] else f"ended after {(info['end'] - info['start']) / 60:.0f} min"
        say(f"session {info['uid']}: {info['owner']} ({info['project']}), started {start}, {state}, "
            f"{info['lines']} lines")
    after = 0

    def show(lines):
        for ln in lines:
            if args.json:
                print(json.dumps(ln))
            elif args.times:
                print(f"{ln['t'] - info['start']:9.3f}  {ln['text']}")
            else:
                print(ln["text"])
        sys.stdout.flush()

    show(r["lines"])
    after = r["lines"][-1]["seq"] if r["lines"] else after
    # page through the rest of a long finished log, or follow a live one
    while True:
        more = len(r["lines"]) >= q["limit"] and not args.lines
        if not more and not (args.follow and r["live"]):
            break
        try:
            r = api.call("/console?" + urllib.parse.urlencode(
                {"session": info["uid"], "after": after, "limit": 10000, "wait": 20 if args.follow else 0}),
                timeout=40)
        except ApiError as e:
            sys.exit(f"[dcm] {e}")
        q["limit"] = 10000
        show(r["lines"])
        after = r["lines"][-1]["seq"] if r["lines"] else after
    return 0


def cmd_unlock(args, cfg):
    if not args.force:
        sys.exit("[dcm] unlock breaks someone else's lock; pass --force if you mean it")
    r = Api(cfg["url"], cfg["token"]).call("/release", {"force": True, "keep": args.keep, "owner": OWNER})
    say("lock broken" if r["released"] else f"nothing to unlock ({r['reason']})")
    return 0


def cmd_project(args, cfg):
    api = Api(cfg["url"], cfg["token"])
    if args.action == "list":
        projects = api.call("/projects")["projects"]
        if args.json:
            print(json.dumps(projects, indent=2))
            return 0
        for p in projects:
            last = time.strftime("%Y-%m-%d", time.localtime(p["last_session"])) if p["last_session"] else "never"
            reg = f"registered by {p['owner']}" if p["registered"] else "unregistered"
            print(f"{p['name']:<30} {p['sessions']:>4} sessions, {p['seconds'] / 3600:6.1f} h, "
                  f"last {last}  ({reg})")
        return 0
    if args.action == "forget":
        if not args.name:
            sys.exit("[dcm] project forget needs the project name")
        api.call("/projects/delete", {"name": args.name, "owner": OWNER})
        say(f"unregistered {args.name!r} (its history stays)")
        return 0
    # init: create or update .dcm.json, then register (or rename) on the server
    path, data = read_project_file()
    if not path:
        git = find_upwards(".git")
        path = os.path.join(os.path.dirname(git) if git else os.getcwd(), PROJECT_FILE)
    name = args.name or data.get("project") or os.path.basename(os.path.dirname(os.path.abspath(path)))
    pid = data.get("id") or uuid.uuid4().hex
    body = {"name": name, "id": pid, "owner": OWNER}
    if args.description is not None:
        body["description"] = args.description
    try:
        proj = api.call("/projects/register", body)
    except ApiError as e:
        sys.exit(f"[dcm] {e}")
    data.update(project=proj["name"], id=pid)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    say(f"project {proj['name']!r} registered; wrote {path} (commit it so everyone on this repo shares the name)")
    return 0


def strip_dashes(argv):
    return argv[1:] if argv and argv[0] == "--" else argv


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"dcm {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def lock_opts(p):
        p.add_argument("-p", "--project",
                       help="label shown to other users (default: the repo's .dcm.json, else the directory name)")
        p.add_argument("--no-wait", action="store_true", help="fail instead of queueing if the DC is busy")
        p.add_argument("--keep", action="store_true",
                       help="leave the DC running afterwards (skips the post-release health check/reset)")
        p.add_argument("--no-reset", action="store_true", help="never power-cycle automatically")

    def run_opts(p):
        lock_opts(p)
        p.add_argument("--reset-first", action="store_true", help="power-cycle for a clean DC before running")
        p.add_argument("--idle-timeout", type=float, default=0, metavar="S",
                       help="treat S seconds without output as a freeze: kill and power-cycle")
        p.add_argument("--timeout", type=float, default=0, metavar="S", help="hard limit on run time")
        p.add_argument("--log", metavar="FILE", help="append all output to FILE")
        p.add_argument("--no-console", action="store_true",
                       help="don't send the output to dcmgr (it's kept per session for dcm console and the GUI)")

    p = sub.add_parser("status", help="show DC state, lock holder, queue and recent events")
    p.add_argument("--json", action="store_true")
    p.add_argument("-n", "--events", type=int, default=8)
    p = sub.add_parser("load", help="run an ELF/BIN with dc-tool-ip under the lock")
    run_opts(p)
    p.add_argument("elf")
    p.add_argument("extra", nargs=argparse.REMAINDER, help="extra dc-tool-ip args after --")
    p = sub.add_parser("run", help="run any command under the lock ({ip} / $DC_IP substituted)")
    run_opts(p)
    p.add_argument("command", nargs=argparse.REMAINDER)
    p = sub.add_parser("shell", help="hold the lock for an interactive shell")
    lock_opts(p)
    sub.add_parser("ip", help="print the DC's IP")
    sub.add_parser("guide", help="print the rig's live setup/usage guide (meant for agents)")
    p = sub.add_parser("reset", help="power-cycle the DC via Home Assistant")
    p.add_argument("--force", action="store_true", help="even if someone else holds the lock")
    p = sub.add_parser("screenshot", help="save the DC's screen from the capture device (no lock needed)")
    p.add_argument("out", nargs="?", default="dc-screenshot.jpg")
    p = sub.add_parser("project", help="name this repo's project (init), or list/forget projects")
    p.add_argument("action", choices=["init", "list", "forget"])
    p.add_argument("name", nargs="?", help="init: the name (default: .dcm.json's, else the directory); "
                                           "forget: the project to unregister")
    p.add_argument("-d", "--description", help="init: a short description")
    p.add_argument("--json", action="store_true", help="list: JSON output")
    p = sub.add_parser("console", help="console output of a run: by default your latest (-f follows it live)")
    p.add_argument("-f", "--follow", action="store_true", help="keep printing a live session's output until it ends")
    p.add_argument("-n", "--lines", type=int, metavar="N", help="only the last N lines")
    p.add_argument("--session", metavar="ID", help="a specific session (ids are shown by dcm and in the GUI)")
    p.add_argument("--any", action="store_true", help="the latest session of anyone, not just yours")
    p.add_argument("-p", "--project", help="the latest session of this project")
    p.add_argument("-t", "--times", action="store_true", help="prefix each line with seconds since the session began")
    p.add_argument("--json", action="store_true", help="one JSON object per line: seq, t (unix time), text")
    p = sub.add_parser("unlock", help="break a stuck lock held by someone else")
    p.add_argument("--force", action="store_true")
    p.add_argument("--keep", action="store_true", help="don't health-check/reset the DC afterwards")

    args = ap.parse_args()
    cfg = load_config()
    try:
        if args.cmd == "run":
            command = strip_dashes(args.command)
            if not command:
                ap.error("run needs a command, e.g. dcm run -- dc-tool-ip -t {ip} -x game.elf")
            return cmd_run(args, cfg, command)
        return {"status": cmd_status, "load": cmd_load, "shell": cmd_shell, "ip": cmd_ip, "guide": cmd_guide,
                "reset": cmd_reset, "unlock": cmd_unlock, "screenshot": cmd_screenshot,
                "project": cmd_project, "console": cmd_console}[args.cmd](args, cfg)
    except ApiError as e:
        sys.exit(f"[dcm] {e}")
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
