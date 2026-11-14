# Changelog

## 1.0.0 (2026-11-14)

First versioned release. `dcmgr_server.py --version`, `dcm --version` and `/status` report it.

### Upgrading from an unversioned install

- Run `sh deploy/install.sh` as before. On first start the server imports `events.jsonl` and the state
  file's session history into the new database (`dcmgr.db`) once; `events.jsonl` is then unused and can be
  deleted. Older events have kind `legacy`, so they can't be filtered by user or project.
- Port 80 now comes from a capability (systemd `AmbientCapabilities`, OpenRC `capabilities`), not the
  host-wide `/etc/sysctl.d/dcmgr.conf`, which is no longer created. An existing one is still applied, and
  can be deleted.
- Clients keep working unchanged; download `/dcm.py` again for `dcm project`, console capture and the
  other 1.0 features.
- The database moves to schema 3 (session ids, console output); this happens automatically on start.
- The server is now the `dcmgr/` package next to `dcmgr_server.py` (still the entry point); `install.sh`
  installs both.
- "Vacate" in the web GUI is now "Force quit", and "Maintenance mode" is "Pause after this session".

### Added

- **Console capture.** `dcm` keeps printing the loader's output and also sends it to dcmgr, which stores it
  per session with each line's time on the server's clock. `dcm console` (`-f` follows a live run), the
  Console card in the GUI, `GET /console` (long-polls) and `POST /console`. `--no-console` opts out.
- **Recordings.** With a capture device, each session is recorded (H.264/AAC MP4) with the wall-clock time
  of its first frame. The session page (`/session/<id>`) plays it next to the console output; clicking a
  line seeks to it, and `GET /sessions/<id>/frame.jpg?t=` cuts the frame at any line. A 5 GB ring
  (`recording_max_gb`) with pinning (`POST /sessions/pin`); `capture_delay_ms` lines the two up.
- **Adjust picture** in the web GUI: crop the capture interactively (drag, resize, type or use the keyboard,
  with a live preview) to centre an off-centre image. Saved as fractions of the source frame and kept across
  restarts; `capture_output_size` (640x480) stays constant so recordings aren't disturbed.
- **Calibrate timing** in the web GUI: measures `capture_delay_ms` with a bundled test program and applies it
  live. One button when the server has a loader (`loader_command`; `install.sh --with-kos-tool` builds
  kos-tool for dc-load-ip 3.x), otherwise a guided download-and-run with `dcm`.
- **Power control without Home Assistant:** `power_command` runs a script of yours with `on`, `off` or
  `state`; `deploy/power-command.example.sh` covers Tasmota and Shelly plugs.
- **Other loaders:** `"probe": "none"` for loaders dcmgr can't check (e.g. serial dcload).
- **Site naming:** `site_name` and `site_description` in the GUI and guide. The guide now describes the rig
  as configured (loader, power control, capture, recordings) and covers Windows and macOS clients.
- **Project registry.** `dcm project init NAME` writes `.dcm.json` at the repo root so everyone on a repo
  shares one unique project name; running it again renames (history follows). `dcm project list` and
  `forget`; `GET /projects`, `POST /projects/register`, `/projects/delete`. Directory-name labels still work;
  one that matches a registered project runs as `NAME~user`.
- **Structured events** (`kind`, `level`, `console`, `owner`, `project`, `by`, ...) in an **SQLite database**
  with `GET /events` and `GET /sessions` filters, `retention_days` (default 365) and `db_file`.
- **Web GUI:** lock and power combined in one Dreamcast card (Force quit, Force quit + power-cycle);
  queue pause after this session or after the queue; an Activity log merging sessions and events,
  colour-coded and filterable by type, project, user and text, with filters in the URL; display options
  (theme, text size, relative times) and collapsible sections.
- **Queue pause modes:** `POST /maintenance {mode: "after_queue"}` lets everyone already waiting have
  their turn first.
- **Packaging:** systemd unit with hardening; `install.sh` supports apk, apt, dnf, zypper and pacman,
  systemd and OpenRC, `--with-capture`, `--no-packages`, `--uninstall`, `--purge`, and checks the service
  answers after installing. Optional `/etc/dcmgr/secrets.env` for both init systems.
- `console_id` config key, recorded on every event and session (groundwork for several consoles).
- Test suite (`python -m unittest discover -s tests`), running against the fake rig in-process.

### Fixed

- Recordings ran at 25 fps with frame times rounded to 40 ms although the capture delivers 30 fps: ffmpeg's
  MJPEG-over-HTTP reader has a fixed 1/25 s time base. The recorder now pipes the shared feed's frames into
  ffmpeg with a 1 ms time base, keeping every frame with its own time.

- `dcmgr.example.json` had drifted from the server's options; a test now keeps it current.

## Before 1.0

Unversioned single-host deployment (October 2026): lock and queue with heartbeats, discovery by MAC and
dcload `VERS` sweep, Home Assistant power-cycling with reset caps, hands-on holds, web GUI, HDMI screen
and audio capture, and the self-describing agent guide.
