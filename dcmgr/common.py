"""Shared configuration defaults, logging and the API error type."""
import logging
import os


log = logging.getLogger("dcmgr")
DEFAULTS = {
    "site_name": "Dreamcast rig",  # shown in the web GUI's title and the agent guide
    "site_description": "",       # a line or two about this rig for the guide and GUI (where it is, what's attached)
    "console_id": "dc1",          # name of this console in events/history (multi-console groundwork)
    "listen": "0.0.0.0",
    "port": 8731,
    "api_token": "",              # shared secret for POST endpoints; "" disables auth
    "dc_ip": "192.168.1.50",      # the IP dcload is supposed to have
    "dc_alt_ips": [],             # other known addresses (e.g. its DHCP reservation), tried before a sweep
    "dc_mac": "",                 # BBA/LAN adapter MAC; enables ARP-based discovery
    "subnet": "192.168.1.0/24",   # where to look when the DC wanders off via DHCP
    "probe": "vers",              # "vers" (asks dcload-ip itself), "ping" (needs dc_mac), or "none" for loaders
                                  # dcmgr can't check (e.g. serial): no discovery or hang detection
    "lease_ttl": 60,              # seconds without a heartbeat before a lease is dropped
    "queue_poll_timeout": 25,     # max long-poll duration for /acquire
    "idle_probe_interval": 30,    # how often to check on the DC while nobody holds it
    "recover_on_release": True,   # after release, make sure dcload is back (reset if not)
    "ha_url": "",                 # e.g. http://homeassistant.local:8123
    "ha_token": "",               # long-lived access token
    "ha_entity": "switch.dreamcast",
    "ha_power_sensor": "",
    "power_command": "",          # instead of Home Assistant: a command run with on, off or state (string or
                                  # list); see dcmgr/power.py and README "Power control"
    "power_command_timeout": 30,        # optional sensor entity reporting the plug's power draw
    "power_off_seconds": 5,
    "boot_grace": 10,             # BIOS + disc boot time before we start looking
    "boot_timeout": 90,
    "max_resets_per_hour": 20,    # hard cap on power-cycles (protects the plug relay)
    "max_failed_boots": 2,        # after this many resets in a row that don't bring dcload back,
                                  # stop resetting automatically (dead disc/GDEMU); manual still works
    "idle_power_off_minutes": 0,  # switch the plug off after this long unused (0 = never);
                                  # it powers back on automatically at the next lock
    "state_file": "dcmgr_state.json",
    "db_file": "",                # SQLite event log + session history; default: dcmgr.db next to state_file
    "events_file": "",            # pre-0.9 JSON-lines event log, imported into db_file once; default:
                                  # events.jsonl next to state_file
    "retention_days": 365,        # delete events, sessions and console output older than this (0 = keep forever)
    "console_max_lines": 200000,  # console lines kept per session; the rest is dropped (with a warning event)
    "extra_ports": [],            # also listen here, e.g. [80] so http://<host>/ serves the agent guide
    "guide_notes": "",            # site notes (string or list of lines) appended to the agent guide
    "guide_notes_file": "",       # Markdown file appended too; re-read on every request (edit live)
    "guide_includes_token": False,  # embed api_token in the served dcm.py (anyone on the LAN gets it)
    "web_controls": True,         # GUI buttons work without a token (trusted LAN); False = read-only page
    # live view from an HDMI capture stick on the DC's VGA out (via a VGA->HDMI adapter); needs ffmpeg
    "capture_device": "",         # V4L2 device, ideally /dev/v4l/by-id/...-video-index0; "" disables
    "capture_size": "",           # resolution requested from the stick, e.g. "1280x720" ("" = its default)
    "capture_output_size": "640x480",  # size of the picture after an interactive crop (web GUI: Adjust picture)
    "capture_filter": "",         # ffmpeg -vf to undo the adapter's scaling, e.g. "crop=960:720,scale=640:480"
    "capture_fps": 30,
    "capture_quality": 5,         # JPEG quality, ffmpeg -q:v (2 = best .. 31 = worst)
    "capture_idle_seconds": 15,   # stop ffmpeg this long after the last viewer leaves
    "capture_ffmpeg": "ffmpeg",
    "capture_input": [],          # raw ffmpeg input args replacing the V4L2 ones (testing), e.g.
                                  # ["-re", "-f", "lavfi", "-i", "testsrc=size=640x480:rate=30"]
    "capture_audio_device": "",   # ALSA device of the stick's audio, e.g. "plughw:CARD=MS2109,DEV=0"; "" disables
    "capture_audio_bitrate": "128k",
    # recordings: each session's picture (and sound) as an MP4 while a capture device is configured
    "record": True,               # record sessions (needs the capture device; false = live view only)
    "recordings_dir": "",         # default: recordings/ next to state_file
    "recording_max_gb": 5,        # delete the oldest recordings beyond this (pinned sessions are kept)
    "recording_min_free_mb": 1024,  # don't start a recording with less free disk than this
    "recording_preset": "superfast",  # x264 preset: faster = less CPU, bigger files
    "recording_crf": 28,          # x264 quality (lower = better and bigger)
    "loader_command": [],         # lets the server load programs itself (web GUI: Calibrate), e.g.
                                  # ["/opt/dcmgr/bin/kos-tool", "-t", "{ip}", "-x", "{file}"]
    "calibration_elf": "",        # default: tools/calibrate/calib.elf next to dcmgr_server.py
    "calibration_timeout": 120,
    "capture_delay_ms": 100,      # how far the captured picture lags the DC (stick + ffmpeg); lines up
                                  # console lines with the recording
    "capture_audio_input": [],    # raw ffmpeg input args replacing the ALSA ones (testing), e.g.
                                  # ["-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
}


LEVELS = {"debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}
LEVEL_NAMES = {v: k for k, v in LEVELS.items()}


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code

# where web/index.html and dcm.py live: the directory above this package
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
