"""The lock, queue, holds, discovery and power control for one Dreamcast."""
import ipaddress
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from . import __version__
from .common import ApiError, LEVELS, log
from .net import arp_lookup, ping, vers_sweep
from .store import PROJECT_ID_RE, Store, _num, check_project_name, clean_project_name
from .calibrate import Calibrator, elf_path
from .capture import AudioFeed, VideoFeed, crop_from_filter
from .power import PowerError, make_power
from .record import Recorder

class Manager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()              # guards lease/queue/state fields
        self.cond = threading.Condition(self.lock)
        self.console_cond = threading.Condition()  # new console output (long-polling readers wait on it)
        self.hw_lock = threading.Lock()           # serialises probing, discovery, power-cycles
        self.lease = None
        self.queue = []
        self.events = deque(maxlen=200)           # recent event records, oldest first (all of them: self.store)
        self.history = deque(maxlen=50)           # recent finished leases, for the GUI
        self.vacated = {}                         # lease token -> (why it ended, when); told on next heartbeat
        self.evicted = {}                         # queue ticket -> (why it was removed, when); told on next poll
        self.drain_until, self.drain_token = 0.0, None  # no grants until a vacated holder has stopped
        self.reset_pending = False                # no grants while a GUI/forced power-cycle runs
        self.maintenance = None                   # {"by", "note", "since", "mode", "allow"} while paused, see
                                                  # set_maintenance
        self.plug = {"state": None, "power": None, "unit": None, "checked": 0.0}
        self.last_activity = time.time()          # for idle power-off
        self.ip = cfg["dc_ip"]
        self.dc_state = "unknown"                 # unknown|ready|checking|offline|off|resetting|booting
        self.dc_version = None
        self.last_seen = None
        self.reset_times = []
        self._last_sweep = 0.0
        self._next_probe = 0.0
        self.reset_error = None                   # why the last power-cycle failed
        self.failed_boots = 0                     # consecutive resets that did not bring dcload back
        state_dir = os.path.dirname(os.path.abspath(cfg["state_file"]))
        self.capture, self.audio = VideoFeed(cfg), AudioFeed(cfg)
        self._legacy_history = []
        self.config_delay_ms = cfg["capture_delay_ms"]
        self.calibrated_delay = None
        self._load()

        self.store = Store(cfg["db_file"] or os.path.join(state_dir, "dcmgr.db"))
        n_ev, n_se = self.store.import_legacy(cfg["events_file"] or os.path.join(state_dir, "events.jsonl"),
                                              self._legacy_history, cfg["console_id"])
        if n_ev or n_se:
            log.info("imported %d events and %d sessions from the old log/state into %s",
                     n_ev, n_se, self.store.path)
        self.recorder = Recorder(cfg, self.store, self.event, self.capture, self.audio, state_dir)
        if self.calibrated_delay is not None:  # the config stays as loaded; the recorder holds the value in use
            self.recorder.delay_ms = self.calibrated_delay
        self.calibrator = Calibrator(self)
        self._pruned = 0.0
        self._prune()
        self.events.extend(self.store.events(limit=self.events.maxlen))
        if self.lease:  # console lines kept arriving after the last save
            self.lease["lines"] = self.store.console_count(self.lease["id"])
        self.history.extend(self.store.sessions(limit=self.history.maxlen))

    def close(self):
        """Release the database (tests and clean shutdown)."""
        if self.lease:
            self.recorder.stop(self.lease["id"])
        self.store.close()

    # persistence

    def _load(self):
        try:
            with open(self.cfg["state_file"]) as f:
                st = json.load(f)
        except (OSError, ValueError):
            return
        self.ip = st.get("ip") or self.ip
        self.lease = st.get("lease")
        if self.lease:  # state written by older versions lacks these
            for k, v in (("manual", False), ("notices", []), ("asks", []), ("id", secrets.token_hex(6))):
                self.lease.setdefault(k, v)
        self.reset_times = st.get("reset_times", [])
        self.failed_boots = st.get("failed_boots", 0)
        self._legacy_history = st.get("history", [])  # kept in the state file before 0.9; imported once
        self.maintenance = st.get("maintenance")
        self.capture.crop = st.get("capture_crop")
        self.calibrated_delay = st.get("capture_delay_ms")  # set from the web GUI's Calibrate; overrides the config
        if self.maintenance:  # written before pause modes existed
            self.maintenance.setdefault("mode", "now")

    def _save(self):
        st = {"ip": self.ip, "lease": self.lease, "reset_times": self.reset_times,
              "failed_boots": self.failed_boots, "maintenance": self.maintenance,
              "capture_crop": self.capture.crop, "capture_delay_ms": self.calibrated_delay}
        tmp = self.cfg["state_file"] + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(st, f)
            os.replace(tmp, self.cfg["state_file"])
        except OSError as e:
            log.warning("could not save state: %s", e)

    def _prune(self):
        days = self.cfg["retention_days"]
        if not days or time.time() - self._pruned < 86400:
            return
        self._pruned = time.time()
        try:
            ev, se = self.store.prune(time.time() - days * 86400)
        except sqlite3.Error as e:
            log.warning("could not prune old events: %s", e)
            return
        if ev or se:
            log.info("deleted %d events and %d sessions older than %d days", ev, se, days)

    def event(self, kind, msg, level=logging.INFO, **fields):
        """Record an event. `kind` is "<area>.<what>" (lease.granted, power.cycle, ...); `msg` is the
        human-readable line. Extra fields (owner, project, by, ...) make the log filterable; None
        values are dropped. Returns the record."""
        log.log(level, msg)
        rec = {"t": time.time(), "kind": kind, "level": logging.getLevelName(level).lower(),
               "console": self.cfg["console_id"], "msg": msg}
        rec.update((k, v) for k, v in fields.items() if v is not None)
        self.events.append(rec)
        try:
            self.store.add_event(rec)
        except sqlite3.Error as e:
            log.warning("could not store event: %s", e)
        return rec

    def _holder(self):
        """owner/project of the current lease, to tag hardware events with whose session they hit."""
        lease = self.lease
        return {"owner": lease["owner"], "project": lease["project"]} if lease else {}

    def query_events(self, kind=None, user=None, project=None, level=None, console=None, q=None,
                     since=None, until=None, limit=200):
        """Filter the whole event log. kind matches exactly or as a dotted prefix ("lease" matches
        "lease.*"), and may list several ("lease,hold"); user matches owner or by; level is a minimum (info < warning < error); q is a
        case-insensitive substring of msg. Returns the newest `limit` matches, oldest first."""
        if level and level not in LEVELS:
            raise ValueError(f"level must be one of {', '.join(LEVELS)}")
        return self.store.events(kind, user, project, level, console, q, _num(since, "since"),
                                 _num(until, "until"), max(1, min(int(limit), 5000)))

    def query_sessions(self, user=None, project=None, console=None, outcome=None, since=None, until=None,
                       limit=50, uid=None):
        """Finished sessions (leases and holds), newest `limit` that match, oldest first. user matches
        the owner or whoever ended it; since/until select sessions overlapping that window."""
        return self.store.sessions(user, project, console, outcome, _num(since, "since"), _num(until, "until"),
                                   max(1, min(int(limit), 5000)), uid)

    @property
    def power_ctl(self):
        """The configured power control (Home Assistant or power_command), or None. Built from the
        config on each use, so a changed config needs no extra bookkeeping."""
        return make_power(self.cfg)

    @property
    def power_enabled(self):
        return self.power_ctl is not None

    # leases (caller holds self.lock for underscore methods)

    def _lease_public(self):
        if not self.lease:
            return None
        lease = self.lease
        return {"id": lease["id"], "owner": lease["owner"], "project": lease["project"],
                "acquired": lease["acquired"], "expires": lease["expires"], "prepared": lease["prepared"],
                "manual": lease["manual"], "asks": lease["asks"], "lines": lease.get("lines", 0)}

    def _grantable(self):
        """Nothing stops the DC being handed out right now (who gets it is _next_entry's call)."""
        return self.lease is None and not self.reset_pending and time.time() >= self.drain_until

    def _allowed(self, entry):
        """Whether a pause still lets this queue entry through ("after_queue" serves those already waiting)."""
        m = self.maintenance
        return not m or (m["mode"] == "after_queue" and entry["ticket"] in m.get("allow", ()))

    def _next_entry(self):
        """The queue entry that gets the DC next, or None (empty queue, or paused)."""
        return next((q for q in self.queue if self._allowed(q)), None)

    def _maint_public(self):
        m = self.maintenance
        if not m:
            return None
        remaining = sum(1 for q in self.queue if self._allowed(q)) if m["mode"] == "after_queue" else 0
        return {"by": m["by"], "note": m["note"], "since": m["since"], "mode": m["mode"], "remaining": remaining}

    def _grant(self, entry):
        now, hold = time.time(), entry.get("hold", False)
        self.lease = {"token": secrets.token_hex(16), "id": secrets.token_hex(6), "owner": entry["owner"],
                      "project": entry["project"], "acquired": now, "prepared": False, "manual": hold,
                      "notices": [], "asks": [], "lines": 0,
                      "expires": now + (entry["minutes"] * 60 if hold else self.cfg["lease_ttl"])}
        self.last_activity = now
        self._save()
        self.recorder.start(self.lease["id"])
        if hold:
            self.event("hold.granted", f"hold granted to {entry['owner']} for {entry['minutes']} min",
                       owner=entry["owner"], project=entry["project"], minutes=entry["minutes"],
                       session=self.lease["id"])
        else:
            self.event("lease.granted", f"lease granted to {entry['owner']} ({entry['project']})",
                       owner=entry["owner"], project=entry["project"], session=self.lease["id"])
        self.cond.notify_all()

    def _end_lease(self, outcome, how, keep=False, level=logging.INFO, recover=True, by=None, reason=None):
        """outcome: released | expired (missed heartbeats) | ended (hold time up) | vacated (by someone else)."""
        lease, self.lease = self.lease, None
        now = time.time()
        session = {"console": self.cfg["console_id"], "owner": lease["owner"], "project": lease["project"],
                   "manual": lease["manual"], "start": lease["acquired"], "end": now, "outcome": outcome,
                   "how": how, "by": by, "reason": reason, "keep": keep, "uid": lease["id"],
                   "lines": lease.get("lines", 0)}
        self.history.append(session)
        try:
            self.store.add_session(session)
        except sqlite3.Error as e:
            log.warning("could not store session: %s", e)
        self.recorder.stop(lease["id"])  # after the row exists: the finished file is attached to it
        self.last_activity = now
        self._save()
        kind = "hold" if lease["manual"] else "lease"
        self.event(f"{kind}.{outcome}", f"{kind} of {lease['owner']} ({lease['project']}) {how}"
                   + (" (DC left running)" if keep else ""), level,
                   owner=lease["owner"], project=lease["project"], by=by, reason=reason, keep=keep or None,
                   duration=round(now - lease["acquired"], 1), session=lease["id"],
                   lines=lease.get("lines") or None)
        self.cond.notify_all()
        with self.console_cond:  # live console readers: the session is over
            self.console_cond.notify_all()
        if recover:
            self._after_release(keep)

    def _vacate(self, by, keep=False, drain=True, reason=None, recover=True):
        """End someone else's lease. A machine holder hears about it on its next heartbeat;
        with drain, nobody else is granted the DC until it has had the chance to stop."""
        lease = self.lease
        why = f"force-released by {by}" + (f": {reason}" if reason else "")
        if not lease["manual"]:
            self.vacated[lease["token"]] = (f"the DC lease was {why}", time.time())
            if drain:
                self.drain_until = time.time() + self.cfg["lease_ttl"] / 3 + 5
                self.drain_token = lease["token"]
        how = (f"released by {by}" if lease["manual"] else f"vacated by {by}") + (f" ({reason})" if reason else "")
        self._end_lease("vacated", how, keep, logging.WARNING, recover, by=by, reason=reason)

    def _expire(self):
        now = time.time()
        if self.lease and self.lease["expires"] < now:
            if self.lease["manual"]:
                self._end_lease("ended", "ended (hold time up)")
            else:
                self._end_lease("expired", "expired - missed heartbeats", level=logging.WARNING)
        poll_grace = self.cfg["queue_poll_timeout"] + 15
        self.queue = [q for q in self.queue
                      if (q["expires"] if q.get("hold") else q["seen"] + poll_grace) >= now]
        for told in (self.vacated, self.evicted):
            for k in [k for k, (_, t) in told.items() if now - t > 600]:
                del told[k]
        if self.drain_until and now >= self.drain_until:
            self.drain_until, self.drain_token = 0.0, None
        # holds have no client polling for them, so the server grants them itself
        nxt = self._next_entry()
        if nxt is not None and nxt.get("hold") and self._grantable():
            self.queue.remove(nxt)
            self._grant(nxt)

    def _require(self, token):
        self._expire()
        if not self.lease or not token or not secrets.compare_digest(self.lease["token"], token):
            raise ApiError(409, "you do not hold the DC lease (expired or released)")

    def _queue_entry(self, qid):
        entry = next((q for q in self.queue if q["id"] == qid), None)
        if entry is None:
            raise ApiError(404, "no such queue entry (it may have just been granted or left)")
        return entry

    def pin(self, uid, pinned, by):
        """Keep a session's recording and console output past recording_max_gb and retention_days."""
        if self.store.session(uid) is None:
            raise ApiError(404, "no such finished session")
        self.store.update_session(uid, pinned=1 if pinned else 0)
        self.event("session.pinned" if pinned else "session.unpinned",
                   f"{by} {'pinned' if pinned else 'unpinned'} session {uid}", by=by, session=uid)
        if not pinned:
            self.recorder.enforce_budget()
        return {"ok": True, "pinned": bool(pinned)}

    # capture picture adjustment

    ADJUST_SECONDS = 600  # adjust mode ends by itself if forgotten

    def capture_settings(self):
        """What the web GUI's adjuster starts from: the crop in effect as fractions of the source frame."""
        cap = self.capture
        if not cap.enabled:
            raise ApiError(404, "no capture device configured")
        self._check_adjust()
        from_config = crop_from_filter(self.cfg["capture_filter"], self.cfg["capture_size"])
        crop = cap.crop or from_config or {"x": 0, "y": 0, "w": 1, "h": 1}
        source = "interactive" if cap.crop else "config" if self.cfg["capture_filter"] else "none"
        return {"crop": crop, "source": source, "adjusting": cap.adjusting,
                "adjust_until": cap.adjust_until if cap.adjusting else None, "filter": cap.filter(),
                "config_filter": self.cfg["capture_filter"], "output": self.cfg["capture_output_size"]}

    def capture_adjust(self, on, by):
        """Show the whole uncropped frame (for the adjuster), or go back to the saved crop."""
        cap = self.capture
        if not cap.enabled:
            raise ApiError(404, "no capture device configured")
        was = cap.adjusting
        cap.adjust_until = time.time() + self.ADJUST_SECONDS if on else 0.0
        if on != was:
            cap.restart()
            if on:
                self.event("capture.adjusting", f"{by} is adjusting the capture picture", by=by)
        return self.capture_settings()

    def capture_crop(self, crop, by, reset=False):
        """Save an interactive crop ({x, y, w, h} fractions of the source frame), or with reset go back to
        capture_filter from the config. Leaves adjust mode."""
        cap = self.capture
        if not cap.enabled:
            raise ApiError(404, "no capture device configured")
        if reset:
            new = None
        else:
            try:
                new = {k: round(float(crop[k]), 5) for k in ("x", "y", "w", "h")}
            except (TypeError, KeyError, ValueError):
                raise ApiError(400, "crop needs x, y, w and h as fractions of the frame (0-1)") from None
            if not (0 <= new["x"] < 1 and 0 <= new["y"] < 1 and 0.1 <= new["w"] <= 1 and 0.1 <= new["h"] <= 1
                    and new["x"] + new["w"] <= 1.0001 and new["y"] + new["h"] <= 1.0001):
                raise ApiError(400, "crop must lie inside the frame and keep at least 10% of each side")
        cap.crop = new
        cap.adjust_until = 0.0
        with self.lock:
            self._save()
        cap.restart()
        if new:
            self.event("capture.cropped", f"{by} adjusted the capture picture (crop {new['w']:.1%} x {new['h']:.1%} "
                       f"at {new['x']:.1%}, {new['y']:.1%})", by=by, crop=new)
        else:
            self.event("capture.cropped", f"{by} reset the capture picture to the configured filter", by=by)
        return self.capture_settings()

    def set_capture_delay(self, ms, by, reset=False):
        """Apply a capture_delay_ms (from Calibrate) live, kept across restarts; reset goes back to the config's."""
        if reset:
            self.calibrated_delay = None
            self.recorder.delay_ms = self.config_delay_ms
            self.event("capture.delay", f"{by} reset capture_delay_ms to the configured {self.config_delay_ms}",
                       by=by, delay_ms=self.config_delay_ms)
        else:
            try:
                ms = int(round(float(ms)))
            except (TypeError, ValueError):
                raise ApiError(400, "delay_ms must be a number of milliseconds") from None
            if not 0 <= ms <= 2000:
                raise ApiError(400, "delay_ms must be between 0 and 2000")
            self.calibrated_delay = ms
            self.recorder.delay_ms = ms
            self.event("capture.delay", f"{by} set capture_delay_ms to {ms}", by=by, delay_ms=ms)
        with self.lock:
            self._save()
        return self.calibrator.status()

    def _check_adjust(self):
        """End a forgotten adjust mode (called from the monitor and on reads)."""
        cap = self.capture
        if cap.adjust_until and not cap.adjusting:
            cap.adjust_until = 0.0
            cap.restart()
            self.event("capture.adjust_timeout", "capture picture adjustment ended without saving (timed out)")

    # console output

    def console_append(self, token, lines, sent=None):
        """The lease holder's loader printed `lines` ([[client_time, text], ...]). Times are moved onto
        this server's clock using `sent` (the client's clock when it sent them), so they line up with
        events and recordings even if the client's clock is off."""
        if not isinstance(lines, list):
            raise ApiError(400, "lines must be a list of [time, text]")
        now = time.time()
        offset = now - float(sent) if sent is not None else 0.0
        with self.lock:
            self._require(token)
            lease = self.lease
            cap = self.cfg["console_max_lines"]
            rows, seq = [], lease.get("lines", 0)
            for item in lines[:5000]:
                if seq >= cap:
                    if not lease.get("truncated"):
                        lease["truncated"] = True
                        self.event("console.truncated", f"console output of {lease['owner']} ({lease['project']}) "
                                   f"cut off after {cap} lines", logging.WARNING, owner=lease["owner"],
                                   project=lease["project"], session=lease["id"])
                    break
                try:
                    t, text = float(item[0]) + offset, str(item[1])
                except (TypeError, ValueError, IndexError):
                    raise ApiError(400, "lines must be a list of [time, text]") from None
                seq += 1
                # never later than now, never before the session started (clock jitter)
                rows.append((seq, min(max(t, lease["acquired"]), now), text[:4000]))
            if rows:
                self.store.add_console(lease["id"], rows)
                lease["lines"] = seq
        if rows:
            with self.console_cond:
                self.console_cond.notify_all()
        return {"ok": True, "lines": seq}

    def _console_session(self, session=None, user=None, project=None):
        """(session info, live) for a session id, or by default the live session (if it matches
        user/project) or else the latest finished one with console output."""
        with self.lock:
            lease = self.lease
            if lease and (session == lease["id"] or not session and (not user or lease["owner"] == user)
                          and (not project or lease["project"] == project)):
                return {"uid": lease["id"], "owner": lease["owner"], "project": lease["project"],
                        "manual": lease["manual"], "start": lease["acquired"], "end": None,
                        "lines": lease.get("lines", 0), "console": self.cfg["console_id"]}, True
        info = (self.store.session(session) if session
                else self.store.latest_session(user=user, project=project, with_lines=True))
        if info is None:
            raise ApiError(404, "no such session" if session else "no session with console output yet")
        return info, False

    def console_read(self, session=None, user=None, project=None, after=0, limit=1000, wait=0.0, tail=False):
        """Console lines of a session (see _console_session) after line `after`. With wait, a live session
        is long-polled until new lines arrive or it ends. tail returns the last `limit` lines."""
        after, limit = max(0, int(after)), max(1, min(int(limit), 10000))
        wait = max(0.0, min(float(wait), 25.0))
        info, live = self._console_session(session, user, project)
        deadline = time.time() + wait
        while True:
            lines = self.store.console(info["uid"], after, limit, tail=bool(tail) and not after)
            if lines or not live or time.time() >= deadline:
                break
            with self.console_cond:
                self.console_cond.wait(min(deadline - time.time(), 5.0))
            with self.lock:
                live = bool(self.lease and self.lease["id"] == info["uid"])
            if not live:  # ended while we waited: pick up anything it printed last, then stop
                lines = self.store.console(info["uid"], after, limit)
                info = self.store.session(info["uid"]) or info
                break
        return {"session": info, "live": live, "lines": lines}

    # projects

    def resolve_project(self, name, pid, owner):
        """The project label a lock request runs under, plus a note for the client (or None).
        Caller holds self.lock.

        With an id (from the repo's .dcm.json) the registry decides: an unknown id registers its
        name, and a known id with a new free name renames the project. Without an id the name is an
        ad-hoc label (usually the directory name); if a registered project already has it, the
        label is qualified with the user (game~alice) so the two don't get mixed up."""
        if pid:
            proj = self.store.project(pid=pid) if PROJECT_ID_RE.fullmatch(str(pid)) else None
            if proj is None:  # first use on this server (400/409 if the .dcm.json is bad or clashes)
                return self._register_project(name, pid, owner)["name"], None
            self.store.update_project(pid, last_used=time.time())
            if not name or name == proj["name"]:
                return proj["name"], None
            try:  # .dcm.json was edited: rename
                renamed = self._register_project(name, pid, owner)["name"]
                return renamed, f"project renamed from {proj['name']!r} to {renamed!r}"
            except ApiError as e:
                return proj["name"], f"not renamed ({e}); still registered as {proj['name']!r}"
        label = clean_project_name(name)
        taken = self.store.project(name=label)
        if not taken:
            return label, None
        user = clean_project_name(owner.split("@", 1)[0])[:20]
        qualified = f"{label[:39 - len(user)]}~{user}"
        return qualified, (f"{label!r} is a registered project (owner {taken['owner']}); running as "
                           f"{qualified!r}. If this is that project, copy its .dcm.json here; otherwise "
                           "name this one with `dcm project init NAME`")

    def register_project(self, name, pid, owner, description=None):
        """Register a project (dcm project init). Idempotent for the same id; renames if the id is
        known under another name; 409 if another project has the name."""
        with self.lock:
            return self._register_project(name, pid, owner, description)

    def _register_project(self, name, pid, owner, description=None):
        name = check_project_name(name)
        if not pid or not PROJECT_ID_RE.fullmatch(str(pid)):
            raise ApiError(400, "bad project id: need 8-64 letters, digits, '-' or '_'")
        taken = self.store.project(name=name)
        if taken and taken["id"] != pid:
            when = time.strftime("%Y-%m-%d", time.localtime(taken["created"]))
            raise ApiError(409, f"project name {taken['name']!r} is already registered by "
                                f"{taken['owner']} ({when}); pick another name")
        proj = self.store.project(pid=pid)
        if proj is None:
            self.store.add_project(pid, name, owner, description)
            self.event("project.registered", f"project {name} registered by {owner}", project=name, by=owner)
        else:
            if proj["name"] != name:
                self.store.rename_project(pid, proj["name"], name)
                self.event("project.renamed", f"project {proj['name']} renamed to {name} by {owner}",
                           project=name, by=owner, old=proj["name"])
            if description is not None:
                self.store.update_project(pid, description=description)
        return self.store.project(pid=pid)

    def delete_project(self, name, by):
        """Forget a registration (its sessions and events stay). Anyone may do this on a trusted LAN."""
        with self.lock:
            proj = self.store.project(name=name)
            if proj is None:
                raise ApiError(404, f"no registered project named {name!r}")
            self.store.delete_project(proj["id"])
            self.event("project.deleted", f"project {proj['name']} unregistered by {by}", logging.WARNING,
                       project=proj["name"], by=by)
        return {"ok": True}

    def acquire(self, owner, project, ticket=None, wait=0.0, project_id=None):
        wait = max(0.0, min(float(wait), self.cfg["queue_poll_timeout"]))
        deadline = time.time() + wait
        with self.cond:
            if ticket in self.evicted:
                raise ApiError(410, self.evicted.pop(ticket)[0])
            entry = next((q for q in self.queue if q["ticket"] == ticket), None) if ticket else None
            if entry is None:
                project, note = self.resolve_project(project, project_id, owner)
                now = time.time()
                entry = {"ticket": ticket or secrets.token_hex(8), "id": secrets.token_hex(4),
                         "owner": owner, "project": project, "note": note, "seen": now, "since": now}
                self.queue.append(entry)
            while True:
                entry["seen"] = time.time()
                self._expire()
                if entry["ticket"] in self.evicted:  # removed while we were waiting
                    raise ApiError(410, self.evicted.pop(entry["ticket"])[0])
                if self._grantable() and self._next_entry() is entry:
                    self.queue.remove(entry)
                    self._grant(entry)
                    return {"granted": True, "token": self.lease["token"], "ttl": self.cfg["lease_ttl"],
                            "ip": self.ip, "project": entry["project"], "note": entry.get("note")}
                if time.time() >= deadline:
                    if wait <= 0 and entry in self.queue:
                        self.queue.remove(entry)
                    pos = self.queue.index(entry) + 1 if entry in self.queue else None
                    return {"granted": False, "ticket": entry["ticket"], "position": pos,
                            "holder": self._lease_public(),
                            "maintenance": None if self._allowed(entry) else self._maint_public(),
                            "draining": self.drain_until > time.time(), "project": entry["project"],
                            "note": entry.get("note")}
                self.cond.wait(min(deadline - time.time(), 1.0))

    def heartbeat(self, token):
        with self.lock:
            if token in self.vacated:
                if token == self.drain_token:  # it knows now; give it a moment to stop its loader
                    self.drain_until = min(self.drain_until, time.time() + 2)
                raise ApiError(409, self.vacated.pop(token)[0])
            self._require(token)
            self.lease["expires"] = time.time() + self.cfg["lease_ttl"]
            notices, self.lease["notices"] = self.lease["notices"], []
            return {"ok": True, "expires": self.lease["expires"], "notices": notices}

    def release(self, token, keep=False, force=False, by="?", drain=True, reason=None):
        with self.cond:
            self._expire()
            if not self.lease:
                return {"released": False, "reason": "not locked"}
            if force and not (token and secrets.compare_digest(self.lease["token"], token)):
                self._vacate(by, keep, drain, reason)
            else:
                self._require(token)
                self._end_lease("released", "released", keep)
        return {"released": True}

    def evict(self, qid, by):
        with self.cond:
            self._expire()
            entry = self._queue_entry(qid)
            self.queue.remove(entry)
            if not entry.get("hold"):
                self.evicted[entry["ticket"]] = (f"removed from the DC queue by {by}", time.time())
            self.event("queue.evicted", f"{entry['owner']} ({entry['project']}) removed from the queue by {by}",
                       logging.WARNING, owner=entry["owner"], project=entry["project"], by=by)
            self.cond.notify_all()
            self._expire()
        return {"ok": True}

    def move(self, qid, to, by):
        with self.cond:
            self._expire()
            entry = self._queue_entry(qid)
            self.queue.remove(entry)
            to = max(0, min(int(to), len(self.queue)))
            self.queue.insert(to, entry)
            self.event("queue.moved", f"{by} moved {entry['owner']} ({entry['project']}) to queue position {to + 1}",
                       owner=entry["owner"], project=entry["project"], by=by, position=to + 1)
            self.cond.notify_all()
            self._expire()
        return {"ok": True, "position": to + 1}

    def hold(self, by, minutes=30, project=None):
        """Hands-on hold for a person at the console. Queues like anyone else, but the
        server keeps it alive (no heartbeats) and it ends on its own after `minutes`."""
        minutes = max(1, min(int(minutes), 240))
        with self.cond:
            self._expire()
            if any(q.get("hold") and q["owner"] == by for q in self.queue):
                raise ApiError(409, "you already have a hold queued")
            if self.lease and self.lease["manual"] and self.lease["owner"] == by:
                raise ApiError(409, "you already hold the DC; extend the hold instead")
            now = time.time()
            entry = {"ticket": secrets.token_hex(8), "id": secrets.token_hex(4), "owner": by,
                     "project": self.resolve_project(project or "hands-on", None, by)[0], "seen": now, "since": now,
                     "hold": True, "minutes": minutes, "expires": now + 3600}
            self.queue.append(entry)
            self._expire()  # grants it right away if the DC is free
            granted = entry not in self.queue
            if not granted:
                pos = self.queue.index(entry) + 1
                self.event("hold.queued", f"{by} queued a {minutes} min hold (position {pos})",
                           owner=by, project=entry["project"], by=by, minutes=minutes, position=pos)
        return {"granted": granted}

    def extend(self, by, minutes=15):
        minutes = max(1, min(int(minutes), 240))
        with self.lock:
            self._expire()
            if not self.lease or not self.lease["manual"]:
                raise ApiError(409, "the DC isn't under a hands-on hold")
            self.lease["expires"] = max(self.lease["expires"], time.time()) + minutes * 60
            self._save()
            self.event("hold.extended", f"{by} extended the hold of {self.lease['owner']} by {minutes} min",
                       **self._holder(), by=by, minutes=minutes)
        return {"ok": True, "expires": self.lease["expires"]}

    def nudge(self, by, message=None):
        with self.lock:
            self._expire()
            if not self.lease:
                raise ApiError(409, "the DC is free")
            note = {"by": by, "msg": (message or "please release the DC when you can")[:300], "t": time.time()}
            self.lease["notices"].append(note)
            self.lease["asks"].append(note)
            self.event("lease.nudged", f"{by} asked {self.lease['owner']} to release the DC",
                       **self._holder(), by=by, message=note["msg"])
        return {"ok": True}

    def set_maintenance(self, on, by, note="", mode="now"):
        """Pause the queue (maintenance mode). Nobody's session is cut short either way.
        mode "now": nobody else gets the DC after the current session.
        mode "after_queue": everyone already waiting still gets their turn; later arrivals wait."""
        if mode not in ("now", "after_queue"):
            raise ApiError(400, "mode must be 'now' or 'after_queue'")
        note = (note or "")[:300]
        with self.cond:
            self._expire()
            if on:
                allow = [q["ticket"] for q in self.queue] if mode == "after_queue" else []
                self.maintenance = {"by": by, "note": note, "since": time.time(), "mode": mode, "allow": allow}
                if mode == "after_queue" and allow:
                    what = f"queue paused by {by} after the {len(allow)} waiting"
                else:
                    what = f"maintenance mode on by {by}"
                self.event("maintenance.on", what + (f": {note}" if note else "") + " - new locks paused",
                           logging.WARNING, by=by, note=note or None, mode=mode, waiting=len(allow) or None)
            elif self.maintenance:
                self.maintenance = None
                self.event("maintenance.off", f"maintenance mode off by {by}", by=by)
            self._save()
            self.cond.notify_all()
            self._expire()
            return {"maintenance": self._maint_public()}

    def _after_release(self, keep):
        if keep or not self.cfg["recover_on_release"]:
            return
        threading.Thread(target=self._recover, daemon=True).start()

    def _recover(self):
        """Post-release check: make sure dcload is waiting for the next user."""
        while time.time() < self.drain_until:  # a vacated holder may still be talking to the DC
            time.sleep(0.5)
        with self.hw_lock:
            with self.lock:
                if self.lease and self.lease["prepared"]:
                    return  # next holder already took the DC; never probe mid-session
                if self.plug["state"] == "off":
                    return
                self.dc_state = "checking"
            for _ in range(3):  # a program that just exited may still be dropping back to dcload
                ver = self._probe(self.ip)
                if ver:
                    self._set_ready(self.ip, ver)
                    return
                time.sleep(1)
            self._ensure_ready(allow_reset=True, reason="post-release check: dcload not responding")

    def prepare(self, token, allow_reset=True):
        """Called by the holder before using the DC: locate it, reset it if needed."""
        with self.lock:
            self._require(token)
        with self.hw_lock:
            ip = self._ensure_ready(allow_reset)
            with self.lock:
                if self.lease and self.lease["token"] == token:
                    self.lease["prepared"] = True
        if ip is None:  # "" is a real answer with probe: none (no address)
            why = self.reset_error if allow_reset and self.power_enabled else "dcload not answering"
            raise ApiError(503, f"DC not reachable: {why}")
        return {"ip": ip, "version": self.dc_version}

    def reset(self, token=None, force=False, reason="manual", by="?", wait=True):
        if not self.power_enabled:
            raise ApiError(501, "power control is not configured (Home Assistant: ha_url/ha_token/ha_entity, or power_command)")
        with self.cond:
            self._expire()
            mine = bool(self.lease and token and secrets.compare_digest(self.lease["token"], token))
            if self.lease and not mine:
                if not force:
                    raise ApiError(409, f"the DC is in use by {self.lease['owner']}; "
                                        "force ends their session")
                self._vacate(by, keep=False, drain=False, reason="power-cycled", recover=False)
            if not mine:
                self.reset_pending = True  # nobody may take the DC until the power-cycle is done

        def run():
            try:
                with self.hw_lock:
                    ok = self._power_cycle(f"{reason}, requested by {by}", manual=True)
                    with self.lock:
                        if mine and self.lease and self.lease["token"] == token:
                            self.lease["prepared"] = True
                return ok
            finally:
                if not mine:
                    with self.cond:
                        self.reset_pending = False
                        self.cond.notify_all()
                        self._expire()

        if not wait:
            threading.Thread(target=run, daemon=True).start()
            return {"started": True}
        if not run():
            raise ApiError(503, self.reset_error)
        return {"ip": self.ip, "version": self.dc_version}

    def power(self, on, by, force=False):
        if not self.power_enabled:
            raise ApiError(501, "power control is not configured (Home Assistant: ha_url/ha_token/ha_entity, or power_command)")
        with self.cond:
            self._expire()
            if not on and self.lease:
                if not force:
                    raise ApiError(409, f"the DC is in use by {self.lease['owner']}; force ends their session")
                self._vacate(by, keep=False, drain=False, reason="powered off", recover=False)
        if not self.hw_lock.acquire(timeout=5):
            raise ApiError(409, "a power-cycle or check is in progress; try again shortly")
        try:
            self.power_ctl.set(on)
        except PowerError as e:
            raise ApiError(502, str(e))
        finally:
            self.hw_lock.release()
        with self.lock:
            self.plug["state"] = "on" if on else "off"
            self.plug["power"] = self.plug["power"] if on else None
            self.dc_state = "booting" if on else "off"
            self.last_activity = time.time()
            if on:  # give it time to boot, then sweep for it
                self._next_probe = time.time() + self.cfg["boot_grace"]
                self._last_sweep = 0.0
        self.event("power.on" if on else "power.off", f"DC powered {'on' if on else 'off'} by {by}", by=by)
        return {"plug": "on" if on else "off"}

    def status(self):
        with self.cond:
            self._expire()
            now = time.time()
            return {
                "version": __version__,
                "dc": {"state": self.dc_state, "ip": self.ip, "version": self.dc_version,
                       "last_seen": self.last_seen},
                "lease": self._lease_public(),
                "queue": [{"id": q["id"], "owner": q["owner"], "project": q["project"],
                           "since": q["since"], "hold": q.get("hold", False), "minutes": q.get("minutes"),
                           "waits": not self._allowed(q)}  # held back by a pause until it's resumed
                          for q in self.queue],
                "maintenance": self._maint_public(),
                "draining_until": self.drain_until if self.drain_until > now else None,
                "reset_pending": self.reset_pending,
                "plug": {k: self.plug[k] for k in ("state", "power", "unit")},
                "power_enabled": self.power_enabled,
                "power_control": self.power_ctl.name if self.power_enabled else None,
                "ha_enabled": self.power_enabled,  # legacy name (clients before 1.0 read it)
                "resets_last_hour": len([t for t in self.reset_times if now - t < 3600]),
                "failed_boots": self.failed_boots,
                "capture": self.capture.status(),
                "audio": self.audio.status(),
                "recording": self.recorder.status(),
                "history": list(self.history)[-20:],
                "events": list(self.events)[-50:],
                "config": {k: self.cfg[k] for k in ("lease_ttl", "idle_power_off_minutes", "web_controls",
                                                    "max_failed_boots", "site_name", "site_description",
                                                    "probe", "console_id")},
                "now": now,
            }

    # hardware (caller holds self.hw_lock)

    def _set_state(self, state):
        with self.lock:
            self.dc_state = state

    def _set_ready(self, ip, version):
        with self.lock:
            if ip != self.ip:
                self.event("dc.ip_changed", f"DC IP changed {self.ip} -> {ip}", logging.WARNING,
                           old_ip=self.ip, ip=ip)
                self.ip = ip
            state = "unchecked" if self.cfg["probe"] == "none" else "ready"
            self.dc_state, self.dc_version, self.last_seen = state, version, time.time()
            self.failed_boots = 0
            self._save()

    def _probe(self, ip):
        if self.cfg["probe"] == "none":
            return "not checked"
        if self.cfg["probe"] == "ping":
            return "icmp" if ping(ip) else None
        return vers_sweep([ip], timeout=1.0).get(ip)

    def _discover(self, full=True):
        if self.cfg["probe"] == "none":  # nothing to look for: the configured address (maybe "") is the answer
            if self.plug["state"] == "off":
                return None
            self._set_ready(self.cfg["dc_ip"], "not checked")
            return self.ip
        cands = list(dict.fromkeys([self.ip, self.cfg["dc_ip"], *self.cfg["dc_alt_ips"]]))
        for ip in cands:
            ver = self._probe(ip)
            if ver:
                self._set_ready(ip, ver)
                return ip
        mac = self.cfg["dc_mac"]
        if mac:
            ip = arp_lookup(mac)
            if ip and ip not in cands:
                ver = self._probe(ip)
                if ver:
                    self._set_ready(ip, ver)
                    return ip
        if full:
            self._last_sweep = time.time()
            hosts = list(ipaddress.ip_network(self.cfg["subnet"], strict=False).hosts())
            if self.cfg["probe"] == "vers":
                found = vers_sweep(hosts, timeout=2.0)
                if found:
                    mac_ip = arp_lookup(mac) if mac else None
                    ip = mac_ip if mac_ip in found else sorted(found)[0]
                    if len(found) > 1:
                        self.event("dc.multiple", f"multiple dcload instances answered: {sorted(found)}; "
                                   f"using {ip}", logging.WARNING, ips=sorted(found), ip=ip)
                    self._set_ready(ip, found[ip])
                    return ip
            elif mac:
                with ThreadPoolExecutor(64) as pool:
                    list(pool.map(lambda h: ping(str(h)), hosts))
                ip = arp_lookup(mac)
                if ip and self._probe(ip):
                    self._set_ready(ip, "icmp")
                    return ip
            if mac:
                nic_ip = arp_lookup(mac)
                if nic_ip:
                    self.event("dc.no_dcload", f"DC network adapter is at {nic_ip} but dcload is not answering "
                               "(program running or crashed?)", ip=nic_ip)
        with self.lock:
            if self.dc_state not in ("resetting", "booting"):
                self.dc_state = "offline"
        return None

    def _ensure_ready(self, allow_reset, reason="dcload not responding"):
        if self.plug["state"] == "off" and allow_reset and self.power_enabled:
            # switched off (idle power-off or by hand): just bring it up
            return self.ip if self._power_cycle("DC was powered off", manual=True) else None
        ip = self._discover(full=True)
        if ip is not None or not allow_reset or not self.power_enabled:
            return ip
        if self._power_cycle(reason):
            return self.ip
        return None

    def _poll_plug(self):
        self.plug["checked"] = time.time()
        ctl = self.power_ctl
        try:
            state, power, unit = ctl.state() if ctl else ("unknown", None, None)
        except (PowerError, KeyError, TypeError, ValueError) as e:
            log.debug("power state poll failed: %s", e)
            state, power, unit = "unknown", None, None
        with self.lock:
            self.plug.update(state=state, power=power, unit=unit)
            if state == "off" and self.dc_state not in ("resetting", "booting"):
                self.dc_state = "off"

    def _power_cycle(self, reason, manual=False):
        ctl = self.power_ctl
        if ctl is None:
            return self._reset_failed("power control is not configured (Home Assistant: ha_url/ha_token/ha_entity, or power_command)")
        now = time.time()
        with self.lock:
            self.reset_times = [t for t in self.reset_times if now - t < 3600]
            if len(self.reset_times) >= self.cfg["max_resets_per_hour"]:
                self.dc_state = "offline"
                return self._reset_failed(f"reset suppressed ({reason}): {len(self.reset_times)} resets "
                                          "in the last hour")
            if not manual and self.failed_boots >= self.cfg["max_failed_boots"]:
                self.dc_state = "offline"
                return self._reset_failed(f"automatic reset suppressed ({reason}): the last "
                                          f"{self.failed_boots} resets didn't bring dcload back - check the "
                                          "boot disc/GDEMU, then run `dcm reset`")
            self.reset_times.append(now)
            self.dc_state = "resetting"
            self._save()
        self.event("power.cycle", f"power-cycling DC via {ctl.name} ({reason})", logging.WARNING,
                   **self._holder(), reason=reason, manual=manual)
        try:
            ctl.set(False)
            time.sleep(self.cfg["power_off_seconds"])
            ctl.set(True)
        except PowerError as e:
            self._set_state("offline")
            return self._reset_failed(str(e))
        with self.lock:
            self.plug["state"] = "on"
            self.dc_state = "booting"
        deadline = now + self.cfg["boot_timeout"]
        time.sleep(self.cfg["boot_grace"])
        while time.time() < deadline:
            if self._discover(full=True) is not None:
                took = time.time() - now
                self.event("power.back", f"DC back at {self.ip} after {took:.0f}s", **self._holder(),
                           ip=self.ip, seconds=round(took, 1))
                return True
            self._set_state("booting")
            time.sleep(2)
        with self.lock:
            self.dc_state = "offline"
            self.failed_boots += 1
            self._save()
        return self._reset_failed(f"DC did not come back within {self.cfg['boot_timeout']}s "
                                  f"({self.failed_boots} failed in a row)")

    def _reset_failed(self, why):
        self.reset_error = why
        self.event("power.failed", why, logging.ERROR, **self._holder())
        return False

    # background monitor

    def start(self):
        threading.Thread(target=self._monitor, daemon=True).start()

    def _idle_power_off(self):
        if not self.hw_lock.acquire(blocking=False):
            return
        try:
            self.power_ctl.set(False)
        except (PowerError, AttributeError) as e:
            log.warning("idle power-off failed: %s", e)
            return
        finally:
            self.hw_lock.release()
        with self.lock:
            self.plug["state"], self.plug["power"], self.dc_state = "off", None, "off"
        self.event("power.idle_off", f"DC powered off after {self.cfg['idle_power_off_minutes']} min unused; "
                   "it powers back on automatically at the next lock")

    def _monitor(self):
        while True:
            with self.cond:
                self._expire()  # leases must time out even if nobody is polling
                busy = self.lease is not None
                idle = not busy and not self.queue and not self.maintenance and not self.reset_pending
            now = time.time()
            self._prune()
            self._check_adjust()
            if self.power_enabled and now - self.plug["checked"] > 15:
                self._poll_plug()
            idle_off = self.cfg["idle_power_off_minutes"]
            if (idle and idle_off and self.plug["state"] == "on"
                    and self.dc_state in ("ready", "offline", "unknown")
                    and now - self.last_activity > idle_off * 60):
                self._idle_power_off()
            # Never probe while someone holds the lease: dcload answers the most recent
            # sender, so a stray packet could hijack a live dc-tool-ip session.
            if (not busy and now >= self._next_probe and self.plug["state"] != "off"
                    and self.hw_lock.acquire(blocking=False)):
                try:
                    full = self.dc_state in ("ready", "booting") or now - self._last_sweep > 300
                    self._discover(full=full)
                except Exception:
                    log.exception("monitor probe failed")
                finally:
                    self.hw_lock.release()
                self._next_probe = time.time() + self.cfg["idle_probe_interval"]
            time.sleep(2)
