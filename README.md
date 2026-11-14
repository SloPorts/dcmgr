# dcmgr: share one Dreamcast between machines

Source and releases: https://github.com/SloPorts/dcmgr

A small server (`dcmgr_server.py`, Python 3.8+, no dependencies) owns a real Dreamcast. Dev machines,
VMs and agents borrow it with the client (`dcm.py`, a single file): one at a time, with a queue, and with
each session's console output (and, with a capture device, its screen) kept for later.

```
 VM1: dcm load game.elf ─┐                    ┌─ power control: Home Assistant, or your own script
 VM2: dcm shell ─────────┼── HTTP ── dcmgr ───┼─ UDP 31313 ── Dreamcast (dcload-ip)
 VM3: dcm run -- ... ────┘   :8731            └─ optional capture device on the DC's video output
```

It's built around dcload-ip over a network adapter (BBA or LAN adapter), which it can find and check
by itself. Other loaders, such as dcload over a serial cable, work through `dcm run` with
`"probe": "none"`: dcmgr then can't detect a hung DC, but the lock, queue, power control, console capture
and recordings all work. Nothing beyond the Dreamcast is required; power control and a capture device each
add features.

## What it does

- **Exclusive lock** with heartbeats. If a VM crashes or is suspended, its lease expires
  after `lease_ttl` seconds. Other users wait in a FIFO queue and see who holds the DC.
- **Finds the DC when its IP changes.** It tries the last known IP first, then the
  configured IP, then an ARP lookup by MAC, then sends dcload's `VERS` command to every
  host in `subnet`. Whichever host answers as dcload is the DC.
- **Power-cycles the DC** through Home Assistant or a command you provide (see Power control) when
  dcload stops answering. It then waits for the boot and finds the DC again, wherever DHCP
  put it. Resets are capped at `max_resets_per_hour`, so a bad boot disc can't cause a
  reset loop.
- **Gets the DC ready for the next user.** After each release it checks that dcload is
  answering, and power-cycles if not. A game you stopped with Ctrl-C keeps running on the
  DC and won't accept the next upload, so without this the next person would hit a dead
  DC. Pass `--keep` to skip it, for example to keep looking at the screen.
- **Never probes while someone holds the lock.** dcload replies to whoever sent the most
  recent packet, so a stray probe in the middle of a session could steal the console
  from a running `dc-tool-ip`.

## Setup

**Server.** Run it on an always-on host on the same L2 segment as the DC. The HA box, a
Pi, or a small LXC all work. A Windows desktop that sleeps does not. It needs Python 3.8+ (with the
standard `sqlite3` module) and nothing else; ffmpeg only for screen capture.

On Linux with systemd or OpenRC, as root:

```sh
git clone https://github.com/SloPorts/dcmgr.git && cd dcmgr   # or unpack a release archive
sh deploy/install.sh                  # install or upgrade; add --with-capture for ffmpeg + v4l-utils
```

To upgrade later, `git pull` (or unpack the new release) and run `sh deploy/install.sh` again.

It installs python3 with the system's package manager (apk, apt, dnf, zypper or pacman), creates a `dcmgr`
system user (in the `video` and `audio` groups when they exist), puts the code in `/opt/dcmgr`, state in
`/var/lib/dcmgr`, and installs the systemd unit (`deploy/dcmgr.service`) or OpenRC service
(`deploy/dcmgr.initd`). On the first install it scans the local subnet for dcload and writes
`/etc/dcmgr/dcmgr.json` with a random API token; it never overwrites an existing config, and installs
ffmpeg on upgrade when that config uses a capture device. It finishes by checking that the service answers.

| | systemd | OpenRC |
|---|---|---|
| Logs | `journalctl -u dcmgr` | `/var/log/dcmgr.log` |
| Restart after editing the config | `systemctl restart dcmgr` | `rc-service dcmgr restart` |
| Port 80 (`extra_ports`) without root | `AmbientCapabilities=CAP_NET_BIND_SERVICE` | `capabilities="^cap_net_bind_service"` |
| Secrets outside the config | `/etc/dcmgr/secrets.env` | same |

`secrets.env` may set `DCMGR_API_TOKEN=` and `DCMGR_HA_TOKEN=` (make it `chmod 640`, group `dcmgr`).
Other options: `--no-packages` (leave the package manager alone), `--uninstall` (remove the service and code,
keep config, state and user), `--purge` (remove those too).

Installs before 1.0 set `net.ipv4.ip_unprivileged_port_start=80` in `/etc/sysctl.d/dcmgr.conf` for port 80.
The service now gets the capability instead; the old file is still applied while it exists and can be
deleted.

Without the installer, or on another OS:

```sh
cp dcmgr.example.json dcmgr.json        # edit IPs, MAC, HA entity (every option: DEFAULTS in dcmgr/common.py)
export DCMGR_HA_TOKEN=...               # HA long-lived token (or put it in the json)
export DCMGR_API_TOKEN=change-me
python3 dcmgr_server.py -c dcmgr.json
```

Status page: `http://<server>:8731/`. JSON: `/status`.

**Each client machine** (Linux, macOS or Windows, Python 3.8+). The server hands out the client with its
address already filled in, so the quickest setup is to download it from there:

```sh
mkdir -p ~/.local/bin && curl -fsSL http://dcmgr-host:8731/dcm.py -o ~/.local/bin/dcm && chmod +x ~/.local/bin/dcm
```

```powershell
Invoke-WebRequest http://dcmgr-host:8731/dcm.py -OutFile $HOME\dcm.py   # Windows
function dcm { python $HOME\dcm.py @args }
```

Or configure a copy yourself with `DCMGR_URL`, `DCMGR_TOKEN` and `DC_TOOL`, or
`~/.config/dcmgr/client.json`: `{"url": "...", "token": "...", "dc_tool": "/opt/toolchains/dc/bin/dc-tool-ip"}`.

**Naming the rig.** `site_name` (default "Dreamcast rig") titles the web GUI and the agent guide;
`site_description` adds a line or two there (where it is, what's attached). `console_id` (default `dc1`) labels
its events and sessions.

## Usage

```sh
dcm status                                  # state, IP, holder, queue, recent events
dcm load build/game.elf                     # = dc-tool-ip -t <ip> -x build/game.elf
dcm load game.elf -- -c romdisk/            # extra dc-tool-ip args after --
dcm run -- dc-tool-ip -t {ip} -x game.elf   # any command; {ip} and $DC_IP are filled in
dcm load game.elf --idle-timeout 20 --log run.log   # watchdog: no output for 20s => freeze => reset
dcm load game.elf --reset-first             # cold boot before running
dcm load game.elf --no-wait                 # fail at once if busy (for CI)
dcm shell                                   # hold the lock interactively; nested dcm reuses it
dcm reset                                   # power-cycle now
dcm unlock --force                          # break a lock someone forgot about
```

In a Makefile: `run: ; dcm load $(TARGET)`.

### Console output

Everything the loader prints (for `dc-tool-ip`, the program's console output) is shown as usual and also sent
to dcmgr, which keeps it per session with the time each line was printed, corrected onto the server's clock.

```sh
dcm console                        # your latest run's output (works after it ended, from any machine)
dcm console -f                     # follow your live run
dcm console -n 50 -t               # last 50 lines, with seconds since the session began
dcm console --any / -p PROJECT / --session ID / --json
dcm load game.elf --no-console     # don't send this run's output
```

API: `POST /console {token, lines: [[time, text], ...], sent}` (lease holder) and
`GET /console?session=&user=&project=&after=&limit=&tail=1&wait=` (long-polls a live session).
`console_max_lines` (default 200,000) caps a session; old output follows `retention_days`.

### Project names

Sessions are labelled with a project name, which the GUI and `/events`/`/sessions` filter on. By default it
is the directory name, which two unrelated repos can share. To give a repo a unique name:

```sh
dcm project init "Space Game"      # at the repo root; writes .dcm.json {"project", "id"}: commit it
dcm project init "Space Game 2"    # later: rename (past sessions and events follow the new name)
dcm project list                   # registered and ad-hoc names with session counts and hours
dcm project forget "Space Game"    # unregister (history stays)
```

`dcm` finds `.dcm.json` in the current directory or any parent, so teammates who clone the repo share the
project. Names are unique ignoring case; `/` may group sub-projects (`game/render`). A repo whose `.dcm.json` is new to the server registers itself on
first use. Without `.dcm.json`, or with `-p NAME`, the name is a free label as before, except that one
matching a registered project runs as `NAME~user` (dcm says so), so another repo's sessions don't get mixed
in. There is no ownership enforcement: on a trusted LAN anyone may rename or forget a project.

Exit codes: the child's exit code, or 124 for timeout/freeze/lost lease, or 130 for Ctrl-C.

## Testing without hardware

`tools/fake_rig.py` simulates dcload on `127.0.0.20` and a Home Assistant whose
power-cycle brings the DC back on `127.0.0.21`. That mimics the DHCP fallback bug. Point
a config at `"dc_ip": "127.0.0.20", "subnet": "127.0.0.16/28", "ha_url":
"http://127.0.0.1:8123", "ha_token": "test-ha-token"`. To simulate a crash or an IP move,
POST to `/_test/freeze` or `/_test/move` on port 8123.

## Development

    python -m unittest discover -s tests          # ~1.5 min, no hardware needed

The tests start the fake rig in-process on per-test loopback addresses (`127.0.N.0/29`), so they
need an OS that lets any `127.x.y.z` be bound: Linux or Windows, not macOS by default. They cover
locking, the queue, holds, discovery, power-cycling, persistence, the HTTP API and the `dcm` client
end to end. Discovery sweeps use real timeouts, which is where most of the run time goes.

`dcmgr_server.py --version` and `dcm --version` print the version (also in `/status`).

## Power control

dcmgr power-cycles a hung DC by switching its mains power: the Dreamcast's power switch latches, so it turns
back on by itself and boots into dcload again. Without power control everything else still works, but a hung
DC needs a person to switch it off and on.

- **Home Assistant:** set `ha_url`, `ha_token` (a long-lived access token) and `ha_entity` (the plug's switch);
  optionally `ha_power_sensor` for its power draw.
- **Anything else** (a Tasmota or Shelly plug used directly, a relay board, a PDU): set `power_command` to a
  program dcmgr runs with `on`, `off` or `state`. `on`/`off` exit 0 when done (non-zero and a message on stderr
  otherwise); `state` prints `on` or `off`, optionally followed by a reading (`on 11.5 W`), and may exit
  non-zero when it can't tell. It runs as the `dcmgr` user with `DCMGR_ACTION` and `DCMGR_CONSOLE` set,
  within `power_command_timeout` seconds. `deploy/power-command.example.sh` is a starting point for Tasmota
  and Shelly plugs. `power_command` takes precedence over the Home Assistant settings.

## Web GUI

Open `http://<server>/` in a browser. It updates live and lets anyone on the LAN:

- **Dreamcast card (lock + power):** state, plug and who has the DC. Ask the holder to release (their
  `dcm` prints the message), **Force quit** (ends the session with a short drain so the old holder's loader
  stops before the next user starts), **Force quit + power-cycle**, power-cycle or switch off/on, plus
  optional power draw (`ha_power_sensor`) and idle auto power-off (`idle_power_off_minutes`; the DC powers
  back on automatically at the next lock) under Details.
- **Hands-on hold:** hold the DC for 15–120 min while a person uses the console. It queues like any
  client, ends on its own, and can be extended or released (optionally leaving the DC running).
- **Queue:** move entries up, down or to the front, or remove them. A removed `dcm` exits with
  "removed from the DC queue by X". **Pause after this session** lets the current holder finish, then
  hands the DC to nobody until someone resumes (formerly "maintenance mode"). **Pause after the queue**
  still gives everyone already waiting their turn; later arrivals are marked "until resume". Neither cuts a
  session short. API: `POST /maintenance {on, note, mode: "now" | "after_queue"}`.
- **Activity:** sessions and events in one list, colour-coded by type (sessions, power and DC, queue,
  projects; problems highlighted). Filter by type, project, user or text; click any project or user name to
  filter on it. Filters are kept in the URL (`/#project=Space%20Game&type=sessions`), so a filtered view
  can be shared as a link.
- **Display:** theme (system, light, dark), text size and clock or relative times, remembered per browser,
  as is which sections (Screen, Activity, Details, Guide) are collapsed. A collapsed Screen stops the live
  view, so the server can stop capturing.
- **Storage:** sessions and the event log are kept in an SQLite database (`dcmgr.db` next to the
  state file, or `db_file`).

Events are structured JSON: `{t, kind, level, console, msg}` plus `owner`, `project`, `by`, `reason` and
similar where they apply. `kind` is `<area>.<what>`: `lease.*`/`hold.*` (`granted`, `released`, `expired`,
`ended`, `vacated`, `nudged`, `queued`, `extended`), `queue.evicted`/`moved`, `maintenance.on`/`off`,
`power.*` (`cycle`, `back`, `failed`, `on`, `off`, `idle_off`, tagged with the session they interrupted) and
`dc.*` (`ip_changed`, `multiple`, `no_dcload`).
`GET /events` filters them: `kind` (exact or prefix, e.g. `power`), `user` (owner or actor), `project`,
`console`, `level` (minimum: `info`, `warning`, `error`), `q` (text), `since`/`until` (Unix time) and
`limit` (default 200, max 5000). `GET /sessions` lists finished sessions with `user`, `project`, `console`,
`outcome` (`released`, `expired`, `ended`, `vacated`), `since`/`until` and `limit` (default 50).
`console_id` in the config (default `dc1`) names the console.

Rows older than `retention_days` (default 365, 0 = forever) are deleted daily. On the first start after
upgrading from a version without the database, the old `events.jsonl` and the state file's session history
are imported once (old events get kind `legacy`); `events.jsonl` is then left alone and can be deleted. To
back up the database while dcmgr runs, use `sqlite3 dcmgr.db ".backup copy.db"` (Alpine: `apk add sqlite`)
rather than copying the file
(it uses WAL, so recent writes may still be in `dcmgr.db-wal`).

The page asks for your name once (kept in the browser) for the event log. There is no login; set
`"web_controls": false` for a read-only page.

## Screen capture and recordings

Optional. Any video capture device ffmpeg can read through V4L2 works (`capture_device`), fed from the DC's
video output: for example the DC's VGA output through a VGA-to-HDMI adapter into a cheap USB HDMI capture
stick, or a composite/S-video capture device. It gives a live view in the GUI, `GET /capture.jpg`,
`GET /capture.mjpg` and `dcm screenshot shot.jpg`, without touching the DC or its network adapter. ffmpeg
runs only while someone watches or a session is recorded, and stops `capture_idle_seconds` after that.

Sound comes from the device's audio input if it has one (`capture_audio_device`, an ALSA device such as
`plughw:CARD=MS2109,DEV=0`): an Unmute button in the GUI plays `/capture.mp3`, and "Open in VLC" downloads
`/capture.m3u`, which plays `/capture.mkv` (picture + sound, remuxed per viewer without re-encoding). The
browser's audio runs ~0.5-1 s behind the picture; VLC keeps both in sync.

**Recordings.** With a capture device, every session (and hands-on hold) is recorded: picture and sound as
H.264/AAC MP4, about 0.5 GB per hour at 640x480, costing roughly 10% of one CPU core. Each recording keeps the
wall-clock time of its first frame, so a console line printed at time T is at video position
T - t0 + `capture_delay_ms` (default 100, the capture chain's lag). The session page (`/session/<id>`, linked
from the Console card and the Activity log) shows the recording next to the console output: click a line to
jump there, or open the screen at that line (`GET /sessions/<id>/frame.jpg?t=<line time>`, cut from the
video on demand). Recordings are deleted oldest first beyond `recording_max_gb` (default 5); pinning a
session keeps its recording and output. `"record": false` keeps the live view without recording.
`recording_preset`/`recording_crf` trade CPU for size, and `recording_min_free_mb` stops recording on a
nearly full disk.

**Adjusting the picture.** If the image isn't centred or has black borders, use **Adjust picture** under the
live view: the screen then shows the whole uncropped frame with a crop box to drag, resize (handles, Alt+arrow
keys) or type in, and a preview of the result. Save applies it at once (it is kept across restarts); Reset
goes back to `capture_filter`. The crop is stored as fractions of the source frame and the result is scaled to
`capture_output_size` (default 640x480), so recordings and viewers carry on through an adjustment. Everyone
watching sees the uncropped frame while someone adjusts; it ends by itself after 10 minutes.
API: `GET /capture/settings`, `POST /capture/adjust {on}`, `POST /capture/crop {crop: {x, y, w, h}}` or
`{reset: true}`.

**Calibrating the timing.** `capture_delay_ms` (default 100) is how far the recorded picture lags the DC, which
lines console lines up with recordings. **Calibrate timing** under the live view measures it: a short test
program (`tools/calibrate/calib.elf`, bundled) flashes the screen and announces each flash on the console, and
dcmgr finds the flashes in the recording and recommends the smallest delay at which every line's frame already
shows its change. **Apply** sets it live (kept across restarts; **Reset to config** undoes it). With a loader on
the server, **Run calibration** does it all in one go: it takes the DC like any user and runs
`loader_command` (e.g. `["/opt/dcmgr/bin/kos-tool", "-t", "{ip}", "-x", "{file}"]`; `install.sh --with-kos-tool`
builds [kos-tool](https://github.com/andressbarajas/kos-tool), the host tool for dc-load-ip 3.x, and sets it).
Otherwise the panel offers `calib.elf` for download and the `dcm` command to run it from a dev machine as project
`calibration`, then analyses that session. `tools/calibrate/analyze.py` does the same from a command line.
API: `GET /calibrate`, `POST /calibrate/run`, `/calibrate/analyze {session}`, `/calibrate/apply {delay_ms}` or
`{reset: true}`, `GET /calibrate/calib.elf`.

Picture settings in the config: `capture_size` (see `v4l2-ctl --list-formats-ext -d /dev/video0`) and
`capture_filter`, an ffmpeg filter to undo an adapter's scaling, e.g. `"crop=960:720,scale=640:480"` for a 4:3
picture pillarboxed in 1280x720 (an interactive crop replaces it while one is saved). A black picture can mean the running program uses a mode the capture chain can't
show (such as 15 kHz output when capturing VGA).

Example: a USB capture stick on a Proxmox host, passed into an LXC container (no VM needed):

    pct set <ctid> -dev0 /dev/video0,gid=27   # 27 = Alpine's video group
    pct set <ctid> -dev1 /dev/snd/controlC1,gid=18 -dev2 /dev/snd/pcmC1D0c,gid=18 -dev3 /dev/snd/timer,gid=18   # audio (card 1)
    pct reboot <ctid>

Then set `capture_device` to the device's path in the CT, and optionally `capture_size`
(see `v4l2-ctl --list-formats-ext -d /dev/video0`) and `capture_filter` to crop the adapter's black bars,
e.g. `"crop=960:720,scale=640:480"` for a 4:3 picture pillarboxed in 1280x720. Only VGA-capable software shows up.

## Self-describing for agents

`GET /` (for non-browser clients), `/llms.txt`, `/AGENTS.md` and `/guide` return a live Markdown
briefing: current DC state and lock holder, rules (always use dcm, never force-unlock without
asking), one-line setup, usage, exit codes and the raw API. `GET /dcm.py` serves the client with
its URL already pointing at this server. Browsers get the status page with the guide below it.

So for a new project it's enough to tell an agent "connect to <server> for hw testing
details". With `"extra_ports": [80]`, plain `http://host/` works (the installed service may bind ports below
1024, see Setup). Add your own `guide_notes` (a string or
a list of lines) for site specifics. `guide_includes_token: true` embeds the API token in the
served client so new machines need no setup, but then anyone on the LAN can use the API.

## Security

dcmgr is made for a trusted LAN. Keep it off the internet and off networks you don't trust.

- There are no user accounts. Anyone who can reach the server can read everything (status, events, console
  output, recordings, the live picture and sound).
- POST requests need the shared `api_token`, but with `"web_controls": true` (the default) the web page includes
  it, so anyone who can load the page can also lock, vacate, power-cycle, pause and calibrate. Set
  `"web_controls": false` for a read-only page. `"guide_includes_token": true` also puts it in the served
  `dcm.py`.
- Plain HTTP: the token and everything else cross the network unencrypted. Put a reverse proxy with TLS and
  authentication in front if the LAN isn't trusted.
- `power_command` and `loader_command` run as the `dcmgr` user with the arguments shown. Only the config
  (root-owned, group `dcmgr`, mode 640) sets them; the API can't.
- The service runs unprivileged, with only `CAP_NET_BIND_SERVICE` (for port 80) and, under systemd, a
  hardened unit.

## Known limits

- **The lock is advisory.** Running `dc-tool-ip` (or any loader) directly skips it. To enforce it, block
  UDP 31313 to the DC on every host except through dcm (for example a firewall rule on
  the router).
- **VM networking.** dcload sends its replies, console output and file I/O back to the
  sending host. NAT'd VMs usually work for plain UDP but can break with long idle gaps.
  Bridged networking is safer. Allow inbound UDP on the VMs' firewalls.
- **`probe: "vers"` needs a dcload-ip with the `VERS` command** (any recent 1.x/2.x). On
  very old builds, use `"probe": "ping"` with `dc_mac` set. Note that a KOS program with
  networking enabled also answers ping, so that mode can't tell "game running" from
  "dcload ready".
- **The DC must boot straight back into dcload** after a power cycle: a dcload CD-R in the
  drive, or GDEMU/MODE set to boot the dcload image. The Dreamcast power switch latches,
  so restoring mains power turns it on again.
- **`"probe": "none"` can't see the loader.** Use it for loaders dcmgr can't talk to (serial dcload);
  there is then no discovery, no check that the DC is ready, and no automatic reset after a release. Set
  `dc_ip` if `{ip}` should still be filled in, and use `--idle-timeout` or `dcm reset` for hangs.
- **The freeze watchdog only works if your program produces output.** It is opt-in
  (`--idle-timeout`), so a quiet game loop won't trigger a false reset.

## License

MIT, see [LICENSE](LICENSE).
