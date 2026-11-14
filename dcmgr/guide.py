"""The live Markdown briefing served to agents and new machines. It describes this site as configured
(loader and probe, power control, capture, recordings), so it stays right on rigs unlike the author's."""
import time
from .common import log

CLIENT_DEFAULTS_LINE = '    cfg = {"url": "http://localhost:8731", "token": "", "dc_tool": "dc-tool-ip"}\n'


def render_guide(mgr, base):
    """Markdown briefing for an agent (or human) that has just been pointed at this server."""
    st, cfg = mgr.status(), mgr.cfg
    dc, lease = st["dc"], st["lease"]
    network = cfg["probe"] != "none"  # dcmgr can find and check the loader (dcload-ip) itself
    when = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "never"
    if not lease:
        holder = "free"
    elif lease["manual"]:
        holder = f"hands-on hold by {lease['owner']} until {when(lease['expires'])}"
    else:
        holder = f"held by {lease['owner']} ({lease['project']}) since {when(lease['acquired'])}"
    queue = ", ".join(f"{q['owner']} ({'hold' if q['hold'] else q['project']})" for q in st["queue"]) or "empty"
    if cfg["guide_includes_token"]:
        token = "Already embedded in the `dcm.py` served here; nothing to configure."
    else:
        token = ("Set `DCMGR_TOKEN`, or put `{\"token\": \"...\"}` in `~/.config/dcmgr/client.json`. "
                 "If neither exists on this machine, ask the user for it. Do not hunt for it elsewhere.")
    if not st["power_enabled"]:
        reset = "NOT available (no power control configured): a hung DC needs a person to switch it off and on"
    elif st["failed_boots"] >= cfg["max_failed_boots"]:
        reset = (f"automatic resets PAUSED: the last {st['failed_boots']} didn't bring the loader back. "
                 "Tell the user; the DC likely needs physical attention")
    else:
        reset = f"available through {st['power_control']} ({st['resets_last_hour']} in the last hour)"
    maint = st["maintenance"]
    maint = (f"\n- **Queue paused** (by {maint['by']}{': ' + maint['note'] if maint['note'] else ''}): "
             + (f"the {maint['remaining']} already queued still get a turn, then " if maint["remaining"] else "")
             + "new locks are paused; `dcm` waits in the queue until it's resumed" if maint else "")
    power = f"\n- Power: {st['plug']['state'] or 'unknown'}" if st["power_enabled"] else ""
    if network:
        loader = (f"- Loader (dcload): **{dc['state']}** at `{dc['ip']}`, {dc['version'] or 'version unknown'}, "
                  f"last seen {when(dc['last_seen'])}")
    else:
        loader = ("- Loader: not checked by dcmgr (`probe: none`); "
                  + (f"`{{ip}}` is `{dc['ip']}`" if dc["ip"] else "`{ip}` is empty"))

    # what the DC is and how programs get onto it
    if network:
        about_loader = ("running dcload-ip. This server keeps track of the DC's IP (it can change), checks that "
                        "dcload is answering")
        rule1 = ("Always go through `dcm`. Never run `dc-tool-ip` (or another loader) against the DC directly and "
                 "never hardcode its IP.\n   The IP can change, and another machine may be mid-session (your "
                 "packets would hijack its console).")
        loader_setup = f"""`dcm load` runs `dc-tool-ip -t <ip> -x <file>`: it needs a `dc-tool-ip` that works with the dcload version
above, on `PATH` (or set `DC_TOOL=/path/to/dc-tool-ip`). For any other loader or tool, use
`dcm run -- <command> ... {{ip}} ...`; the lock, discovery and recovery work the same.
The machine must be reachable *from* the DC over UDP (bridged networking, inbound UDP allowed),
because dcload sends console output and file I/O back to the host that sent the program."""
    else:
        about_loader = ("with a loader this server doesn't talk to directly (for example dcload over a serial "
                        "cable). This server")
        rule1 = ("Always go through `dcm`: run the loader with `dcm run -- <loader> ...` so you hold the lock "
                 "while you use the DC.")
        loader_setup = """Run your loader with `dcm run -- <loader command> ...` (`dcm load` assumes `dc-tool-ip`). dcmgr doesn't
check the loader itself here, so it can't tell when the DC hangs: use `--idle-timeout` to have a silent
run treated as a hang, or `dcm reset` when you know it hung."""
    if st["power_enabled"]:
        about_power = "and can switch the DC's power to reset it when it hangs."
    else:
        about_power = "and hands out the DC to one machine at a time (it has no power control here)."
    recover = ("6. After you release, the manager checks that dcload is back and power-cycles if not. Use `--keep`\n"
               "   only when a human needs to look at the screen afterwards."
               if network and st["power_enabled"] and cfg["recover_on_release"] else
               "6. After you release, nothing checks the DC for you here: if your run hung it, say so, or run "
               "`dcm reset` before releasing\n   (when power control is available).")

    audio = (f"; sound as MP3: `{base}/capture.mp3`; both in one stream for VLC: `{base}/capture.mkv`"
             if st["audio"]["enabled"] else "")
    capture = "" if not st["capture"]["enabled"] else f"""## Screen capture

The DC's video output is captured on this server, so you can see the screen without touching the DC.
Don't take screenshots through the loader or the DC's network adapter: that link is slow and stalls the game.
`dcm screenshot shot.jpg` (or `GET {base}/capture.jpg`) saves the current frame. It needs no lock.
Live MJPEG stream: `{base}/capture.mjpg`{audio}. A black or missing picture can mean the program uses a video
mode the capture chain doesn't show (for example 15 kHz RGB or composite when the DC's VGA output is
captured), not that it hung.

"""
    rec = st["recording"]
    recording = "" if not rec["enabled"] else f"""## Recordings: what the screen showed at each console line

Every session is recorded (picture{' and sound' if st['audio']['enabled'] else ''}) and kept with its console
output. `dcm console` prints the session id; `{base}/session/<id>` shows the recording next to the output
(click a line to see the screen at that moment). To check the screen at a particular line yourself:
`dcm console --json` gives each line's time `t`, and `GET {base}/sessions/<id>/frame.jpg?t=<t>` returns the
frame on screen then (once the session has ended). Old recordings are deleted beyond
{cfg['recording_max_gb']} GB unless pinned in the web GUI.

"""
    events = "\n".join(f"- {when(e['t'])}  {e['msg']}" for e in st["events"][-8:]) or "- (none yet)"
    notes = cfg["guide_notes"]
    notes = "\n".join(f"- {n}" for n in notes) if isinstance(notes, list) else notes
    if cfg["guide_notes_file"]:
        try:
            with open(cfg["guide_notes_file"], encoding="utf-8") as f:
                notes = "\n\n".join(n for n in (notes, f.read().strip()) if n)
        except OSError as e:
            log.warning("could not read guide_notes_file: %s", e)
    description = f"\n{cfg['site_description'].strip()}\n" if cfg["site_description"].strip() else ""
    return f"""# {cfg['site_name']} (dcmgr)
{description}
You have reached the manager for a real Sega Dreamcast {about_loader}, {about_power}
Several machines share it: this server hands out an exclusive lock and keeps each session's console output.
Everything below is generated live. Humans manage it from a browser at {base}/.

## Current state ({when(time.time())})

{loader}
- Lock: **{holder}**; queue: {queue}
- Power-cycle: {reset}{power}{maint}

## Rules

1. {rule1}
2. Hold the lock only while you need it. `dcm load` and `dcm run` take it and release it for you.
3. Never `dcm unlock --force` or `dcm reset --force` without asking the user. Both end someone else's session.
4. If the DC hangs *while you hold the lock*, `dcm reset` is fine. Inside `dcm run` or `dcm shell` it uses your lock.
5. If `dcm` says it is queued, another machine (or a person, shown as a hold) is using the DC. Wait, or
   ask the user. Don't break the lock.
{recover}
7. Prefer runs that end on their own by exiting back to the loader (for example, test builds that quit after
   N frames). A run cut off by `--timeout`, `--idle-timeout` or Ctrl-C leaves the game running, so the next
   user may need a power-cycle first.
8. If `dcm` prints `message from <someone>`, a person is asking for the DC: finish the current step and release.
9. If `dcm` says your lease was force-released or you were removed from the queue, a person did that on
   purpose. Don't retry straight away; tell the user.

## One-time setup on a machine

The client is one Python file (3.8+, no dependencies). Linux or macOS:

```sh
mkdir -p ~/.local/bin && curl -fsSL {base}/dcm.py -o ~/.local/bin/dcm && chmod +x ~/.local/bin/dcm
dcm status
```

Windows (PowerShell):

```powershell
Invoke-WebRequest {base}/dcm.py -OutFile $HOME\\dcm.py
function dcm {{ python $HOME\\dcm.py @args }}   # add to $PROFILE to keep it
dcm status
```

The downloaded client already points at `{base}`. API token: {token}

{loader_setup}

Once per repo, if it has no `.dcm.json` yet: `dcm project init "Project Name"` (at the repo root; commit the
`.dcm.json` it writes). Sessions are then labelled with that unique name instead of the directory name.

## Usage

```sh
dcm status                                     # state, IP, lock holder, queue, recent events
dcm load build/game.elf                        # lock, find DC, dc-tool-ip -t <ip> -x game.elf, unlock
dcm load game.elf -- -c romdisk/               # extra dc-tool-ip args go after --
dcm run -- dc-tool-ip -t {{ip}} -x game.elf      # any command; {{ip}} and $DC_IP are substituted
dcm load game.elf --idle-timeout 20 --log run.log   # no output for 20s = hang: kill (and power-cycle)
dcm load game.elf --timeout 120                # hard time limit (good for automated tests)
dcm load game.elf --reset-first                # cold boot before running
dcm load game.elf --no-wait                    # fail at once instead of queueing
dcm shell                                      # hold the lock interactively; nested dcm reuses it
dcm reset                                      # power-cycle (only while holding the lock, or when free)
dcm console                                    # what your last run printed (kept on the server per session)
dcm console -f                                 # follow your live run's output (-n 50: last 50 lines; --any: anyone's)
dcm project init "Name"                        # name this repo's project once (writes .dcm.json)
dcm project list                               # projects, sessions and hours on the DC
dcm guide                                      # print this document
```

Exit codes: the program's own exit code, 124 = timeout, hang or lost lock, 130 = interrupted.

Console output: dcm reads the loader's output through a pipe, prints it, and also keeps a timestamped copy
on this server per session (`dcm console`, the web GUI; `--no-console` opts out), so you can look back at a
run after it ended or from another machine. Output buffering: a loader that buffers piped output can look hung
(and get power-cycled with `--idle-timeout`) or print late. dcm sets `PYTHONUNBUFFERED=1` and, on Linux,
wraps the command in `stdbuf -o0`. Loaders with their own buffering (Go, Rust, Java, .NET, or C on
Windows) must flush their output themselves.
A program that never exits (a game loop) keeps running until `--timeout`, `--idle-timeout` or Ctrl-C.

{capture}{recording}## Raw HTTP API (if you can't use dcm)

`GET /status` (JSON, no auth). `GET /events?kind=lease&user=<name>&project=<p>&level=warning&q=<text>&limit=200`
is the filterable event log (kind matches a prefix like `lease` or `power`); `GET /sessions?user=&project=&outcome=&uid=`
lists finished sessions; `GET /projects` lists projects; `GET /console?session=<id>&after=<seq>&wait=20` reads
(and long-polls) a session's output. POST endpoints take JSON and `Authorization: Bearer <token>`:
`/acquire {{owner, project, wait}}` returns `{{granted, token}}`, or `{{ticket, position}}` to re-poll with
(HTTP 410 = removed from the queue by a person);
`/heartbeat {{token}}` at least every {cfg['lease_ttl'] // 3}s (the lease expires after {cfg['lease_ttl']}s);
its reply may carry `notices` (release requests), and HTTP 409 means the lease is gone;
`/prepare {{token}}` locates or resets the DC and returns `{{ip}}`; `/console {{token, lines: [[t, text]], sent}}`;
`/release {{token, keep}}`; `/reset {{token}}`.

## Recent events

{events}
""" + (f"\n## Site notes\n\n{notes}\n" if notes else "")
