"""SQLite storage: event log, session history and the project registry."""
import json
import re
import sqlite3
import threading
import time
from .common import ApiError, LEVELS, LEVEL_NAMES

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, t REAL NOT NULL, kind TEXT NOT NULL, level INTEGER NOT NULL,
    console TEXT, owner TEXT, project TEXT, actor TEXT, msg TEXT NOT NULL,
    data TEXT                     -- JSON object with any other fields
);
CREATE INDEX IF NOT EXISTS events_t ON events (t);
CREATE INDEX IF NOT EXISTS events_project ON events (project, t);
CREATE INDEX IF NOT EXISTS events_owner ON events (owner, t);
CREATE INDEX IF NOT EXISTS events_actor ON events (actor, t);
CREATE INDEX IF NOT EXISTS events_kind ON events (kind, t);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY, console TEXT, owner TEXT, project TEXT, manual INTEGER NOT NULL,
    started REAL NOT NULL, ended REAL NOT NULL, outcome TEXT, how TEXT, actor TEXT, reason TEXT,
    keep INTEGER NOT NULL,
    uid TEXT,                     -- public session id (v3); console output and recordings refer to it
    lines INTEGER NOT NULL DEFAULT 0, recording TEXT, pinned INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS console_lines (  -- what the loader printed, per session (v3)
    id INTEGER PRIMARY KEY, session TEXT NOT NULL, seq INTEGER NOT NULL, t REAL NOT NULL, text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS console_lines_session ON console_lines (session, seq);
CREATE INDEX IF NOT EXISTS console_lines_t ON console_lines (t);
CREATE INDEX IF NOT EXISTS sessions_ended ON sessions (ended);
CREATE INDEX IF NOT EXISTS sessions_project ON sessions (project, ended);
CREATE INDEX IF NOT EXISTS sessions_owner ON sessions (owner, ended);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,          -- random id from the project's .dcm.json
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    owner TEXT, description TEXT, created REAL NOT NULL, last_used REAL
);
"""
SCHEMA_VERSION = 3  # 2: projects table; 3: session uid/lines/recording/pinned, console_lines
# upgrades for tables that already exist (new tables come from SCHEMA's CREATE ... IF NOT EXISTS)
# (table, column, definition): added only where missing, since a table SCHEMA just created has them already
MIGRATIONS = {
    3: [("sessions", "uid", "TEXT"),
        ("sessions", "lines", "INTEGER NOT NULL DEFAULT 0"),
        ("sessions", "recording", "TEXT"),
        ("sessions", "pinned", "INTEGER NOT NULL DEFAULT 0")],
}
# indexes on columns that a migration may have just added
POST_MIGRATION = "CREATE INDEX IF NOT EXISTS sessions_uid ON sessions (uid);"


def _num(v, name):
    """Optional float from a query parameter."""
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None


def _like_prefix(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ".%"


PROJECT_NAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._ /-]{0,38}[A-Za-z0-9._-])?")
PROJECT_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")


def check_project_name(name):
    """Validate a name someone is registering (strict: 1-40 of letters, digits, . _ - / and space;
    '/' groups sub-projects, e.g. game/render)."""
    name = (name or "").strip()
    if not PROJECT_NAME_RE.fullmatch(name):
        raise ApiError(400, f"bad project name {name!r}: use 1-40 letters, digits, '.', '_', '-', '/' or spaces, "
                            "starting with a letter or digit")
    return name


def clean_project_name(name):
    """Make an ad-hoc label (usually a directory name) safe to show and filter on. '~' is reserved
    for names qualified by user (see Manager.resolve_project)."""
    name = re.sub(r"[^A-Za-z0-9._ /-]+", "-", str(name or "")).strip(" ./-_")[:40].strip(" /")
    return name or "unnamed"


def legacy_outcome(how):
    """Session outcome for history entries written before outcomes were recorded."""
    how = how or ""
    if how.startswith(("vacated", "released by")):
        return "vacated"
    if how.startswith("expired"):
        return "expired"
    if how.startswith("ended"):
        return "ended"
    return "released"


class Store:
    """Event log and session history in SQLite (stdlib). One connection shared by all threads and
    serialised by a lock; writes are a handful per session, so this is never the bottleneck."""

    EVENT_CORE = ("t", "kind", "level", "console", "owner", "project", "by", "msg")

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            try:
                self.db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass  # some filesystems can't do WAL; the default journal is fine at this write rate
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript(SCHEMA)
            row = self.db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is None:
                self.db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            elif int(row[0]) > SCHEMA_VERSION:
                self.db.close()
                raise RuntimeError(f"{path} was written by a newer dcmgr (schema {row[0]}); upgrade dcmgr")
            elif int(row[0]) < SCHEMA_VERSION:  # new tables were created by SCHEMA above
                self.db.execute("BEGIN")
                try:
                    for v in range(int(row[0]) + 1, SCHEMA_VERSION + 1):
                        for table, col, decl in MIGRATIONS.get(v, []):
                            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
                            if col not in have:
                                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                    self.db.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),))
                    self.db.execute("COMMIT")
                except BaseException:
                    self.db.execute("ROLLBACK")
                    self.db.close()
                    raise
            self.db.executescript(POST_MIGRATION)

    def close(self):
        with self.lock:
            self.db.close()

    def meta(self, key):
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    # writes

    @classmethod
    def _event_row(cls, e):
        data = {k: v for k, v in e.items() if k not in cls.EVENT_CORE}
        return (e["t"], e["kind"], LEVELS.get(e.get("level"), 1), e.get("console"), e.get("owner"),
                e.get("project"), e.get("by"), e["msg"], json.dumps(data) if data else None)

    @staticmethod
    def _session_row(h):
        return (h.get("console"), h["owner"], h["project"], int(bool(h.get("manual"))), h["start"], h["end"],
                h.get("outcome") or legacy_outcome(h.get("how")), h.get("how"), h.get("by"), h.get("reason"),
                int(bool(h.get("keep"))), h.get("uid"), int(h.get("lines") or 0), h.get("recording"),
                int(bool(h.get("pinned"))))

    SESSION_COLS = ("console, owner, project, manual, started, ended, outcome, how, actor, reason, keep, "
                    "uid, lines, recording, pinned")

    def add_event(self, e):
        with self.lock:
            self.db.execute("INSERT INTO events (t, kind, level, console, owner, project, actor, msg, data) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", self._event_row(e))

    def add_session(self, h):
        with self.lock:
            self.db.execute(f"INSERT INTO sessions ({self.SESSION_COLS}) VALUES ({', '.join('?' * 15)})",
                            self._session_row(h))

    def import_legacy(self, events_path, history, console):
        """One-time import of the pre-SQLite event log (JSON lines) and the state file's history.
        Returns (events, sessions) imported; (0, 0) once done."""
        if self.meta("legacy_imported"):
            return 0, 0
        events = []
        try:
            with open(events_path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict) or not isinstance(e.get("t"), (int, float)) or "msg" not in e:
                continue
            # lines written before events were structured only have t and msg
            e.setdefault("kind", "legacy")
            e.setdefault("level", "info")
            e.setdefault("console", console)
            events.append(self._event_row(e))
        sessions = []
        for h in history or []:
            if isinstance(h, dict) and all(k in h for k in ("owner", "project", "start", "end")):
                sessions.append(self._session_row(dict(h, console=h.get("console") or console)))
        with self.lock:
            self.db.execute("BEGIN")
            try:
                self.db.executemany("INSERT INTO events (t, kind, level, console, owner, project, actor, msg, data) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", events)
                self.db.executemany(f"INSERT INTO sessions ({self.SESSION_COLS}) VALUES ({', '.join('?' * 15)})",
                                    sessions)
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('legacy_imported', ?)", (str(time.time()),))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return len(events), len(sessions)

    def prune(self, before):
        with self.lock:
            ev = self.db.execute("DELETE FROM events WHERE t < ?", (before,)).rowcount
            # pinned sessions keep their row and console output
            se = self.db.execute("DELETE FROM sessions WHERE ended < ? AND NOT pinned", (before,)).rowcount
            self.db.execute("DELETE FROM console_lines WHERE t < ? AND session NOT IN "
                            "(SELECT uid FROM sessions WHERE pinned AND uid IS NOT NULL)", (before,))
        return ev, se

    # projects (callers serialise registry changes with Manager.lock)

    def project(self, pid=None, name=None):
        """The registered project with this id, or with this name (any case)."""
        col, v = ("id", pid) if pid else ("name", name)
        with self.lock:
            row = self.db.execute(f"SELECT * FROM projects WHERE {col} = ?", (v,)).fetchone()
        return dict(row) if row else None

    def add_project(self, pid, name, owner, description=None):
        with self.lock:
            self.db.execute("INSERT INTO projects (id, name, owner, description, created) VALUES (?, ?, ?, ?, ?)",
                            (pid, name, owner, description, time.time()))

    def rename_project(self, pid, old, new):
        """Rename, and carry the project's past sessions and events over to the new name."""
        with self.lock:
            self.db.execute("BEGIN")
            try:
                self.db.execute("UPDATE projects SET name = ? WHERE id = ?", (new, pid))
                self.db.execute("UPDATE sessions SET project = ? WHERE project = ?", (new, old))
                self.db.execute("UPDATE events SET project = ? WHERE project = ?", (new, old))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def update_project(self, pid, **fields):
        cols = [k for k in ("description", "last_used") if k in fields]
        if cols:
            with self.lock:
                self.db.execute(f"UPDATE projects SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                                [fields[c] for c in cols] + [pid])

    def delete_project(self, pid):
        with self.lock:
            self.db.execute("DELETE FROM projects WHERE id = ?", (pid,))

    def projects(self):
        """Registered projects with usage stats, then names seen in sessions that aren't registered."""
        with self.lock:
            reg = self.db.execute(
                "SELECT p.*, COUNT(s.id) AS sessions, MAX(s.ended) AS last_session, "
                "COALESCE(SUM(s.ended - s.started), 0) AS seconds "
                "FROM projects p LEFT JOIN sessions s ON s.project = p.name COLLATE NOCASE "
                "GROUP BY p.id ORDER BY COALESCE(MAX(s.ended), p.last_used, p.created) DESC").fetchall()
            adhoc = self.db.execute(
                "SELECT project AS name, COUNT(*) AS sessions, MAX(ended) AS last_session, "
                "SUM(ended - started) AS seconds FROM sessions "
                "WHERE project NOT IN (SELECT name FROM projects) "
                "GROUP BY project ORDER BY MAX(ended) DESC LIMIT 200").fetchall()
        out = [dict(r, registered=True) for r in reg]
        out += [dict(r, registered=False) for r in adhoc]
        for p in out:
            p["seconds"] = round(p["seconds"] or 0)
        return out

    # reads

    @staticmethod
    def _event_dict(r):
        e = {"t": r["t"], "kind": r["kind"], "level": LEVEL_NAMES.get(r["level"], "info"),
             "console": r["console"], "msg": r["msg"]}
        for col, key in (("owner", "owner"), ("project", "project"), ("actor", "by")):
            if r[col] is not None:
                e[key] = r[col]
        if r["data"]:
            e.update(json.loads(r["data"]))
        return e

    @staticmethod
    def _session_dict(r):
        return {"console": r["console"], "owner": r["owner"], "project": r["project"],
                "manual": bool(r["manual"]), "start": r["started"], "end": r["ended"], "outcome": r["outcome"],
                "how": r["how"], "by": r["actor"], "reason": r["reason"], "keep": bool(r["keep"]),
                "uid": r["uid"], "lines": r["lines"], "pinned": bool(r["pinned"]),
                "recording": json.loads(r["recording"]) if r["recording"] else None}

    def session(self, uid):
        """A finished session by its public id, or None."""
        with self.lock:
            row = self.db.execute("SELECT * FROM sessions WHERE uid = ?", (uid,)).fetchone()
        return self._session_dict(row) if row else None

    def latest_session(self, user=None, project=None, with_lines=False):
        """The most recently finished session (of this user/project, optionally only ones with console output)."""
        where, args = ["uid IS NOT NULL"], []
        if user:
            where.append("owner = ?")
            args.append(user)
        if project:
            where.append("project = ?")
            args.append(project)
        if with_lines:
            where.append("lines > 0")
        with self.lock:
            row = self.db.execute(f"SELECT * FROM sessions WHERE {' AND '.join(where)} "
                                  "ORDER BY ended DESC, id DESC LIMIT 1", args).fetchone()
        return self._session_dict(row) if row else None

    def pinned_uids(self):
        with self.lock:
            return {r[0] for r in self.db.execute("SELECT uid FROM sessions WHERE pinned AND uid IS NOT NULL")}

    def update_session(self, uid, **fields):
        cols = [k for k in ("recording", "pinned", "lines") if k in fields]
        if cols:
            with self.lock:
                self.db.execute(f"UPDATE sessions SET {', '.join(c + ' = ?' for c in cols)} WHERE uid = ?",
                                [fields[c] for c in cols] + [uid])

    # console output

    def add_console(self, session, rows):
        """rows: [(seq, t, text), ...]"""
        with self.lock:
            self.db.executemany("INSERT INTO console_lines (session, seq, t, text) VALUES (?, ?, ?, ?)",
                                [(session, seq, t, text) for seq, t, text in rows])

    def console(self, session, after=0, limit=1000, tail=False):
        """Lines with seq > after, oldest first; with tail, the last `limit` lines instead."""
        with self.lock:
            if tail:
                rows = self.db.execute("SELECT seq, t, text FROM console_lines WHERE session = ? AND seq > ? "
                                       "ORDER BY seq DESC LIMIT ?", (session, after, limit)).fetchall()
                rows.reverse()
            else:
                rows = self.db.execute("SELECT seq, t, text FROM console_lines WHERE session = ? AND seq > ? "
                                       "ORDER BY seq LIMIT ?", (session, after, limit)).fetchall()
        return [{"seq": r[0], "t": r[1], "text": r[2]} for r in rows]

    def console_count(self, session):
        with self.lock:
            row = self.db.execute("SELECT COALESCE(MAX(seq), 0) FROM console_lines WHERE session = ?",
                                  (session,)).fetchone()
        return row[0]

    def events(self, kind=None, user=None, project=None, level=None, console=None, q=None,
               since=None, until=None, limit=200):
        where, args = [], []
        kinds = [k.strip() for k in (kind or "").split(",") if k.strip()]
        if kinds:  # each matches exactly or as a dotted prefix; several are ORed
            where.append("(" + " OR ".join(["kind = ? OR kind LIKE ? ESCAPE '\\'"] * len(kinds)) + ")")
            for k in kinds:
                args += [k, _like_prefix(k)]
        if user:
            where.append("(owner = ? OR actor = ?)")
            args += [user, user]
        for col, v in (("project", project), ("console", console)):
            if v:
                where.append(f"{col} = ?")
                args.append(v)
        if level:
            where.append("level >= ?")
            args.append(LEVELS[level])
        if q:
            where.append("instr(lower(msg), ?) > 0")
            args.append(q.lower())
        if since is not None:
            where.append("t >= ?")
            args.append(since)
        if until is not None:
            where.append("t <= ?")
            args.append(until)
        sql = ("SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY t DESC, id DESC LIMIT ?")
        with self.lock:
            rows = self.db.execute(sql, args + [limit]).fetchall()
        return [self._event_dict(r) for r in reversed(rows)]

    def sessions(self, user=None, project=None, console=None, outcome=None, since=None, until=None, limit=50,
                 uid=None):
        where, args = [], []
        if user:
            where.append("(owner = ? OR actor = ?)")
            args += [user, user]
        for col, v in (("project", project), ("console", console), ("outcome", outcome), ("uid", uid)):
            if v:
                where.append(f"{col} = ?")
                args.append(v)
        if since is not None:
            where.append("ended >= ?")
            args.append(since)
        if until is not None:
            where.append("started <= ?")
            args.append(until)
        sql = ("SELECT * FROM sessions" + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY ended DESC, id DESC LIMIT ?")
        with self.lock:
            rows = self.db.execute(sql, args + [limit]).fetchall()
        return [self._session_dict(r) for r in reversed(rows)]
