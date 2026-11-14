"""Switching the Dreamcast's power: through Home Assistant, or through a command the site manages itself
(a relay board, a PDU, another smart plug's CLI...). Power control is optional; without it dcmgr can't
reset a hung DC, but everything else works."""
import json
import os
import shlex
import subprocess
import urllib.request


class PowerError(Exception):
    pass


class HomeAssistant:
    name = "Home Assistant"

    def __init__(self, cfg):
        self.cfg = cfg

    def _request(self, path, body=None):
        req = urllib.request.Request(
            f"{self.cfg['ha_url'].rstrip('/')}{path}", method="GET" if body is None else "POST",
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.cfg['ha_token']}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001 - urllib raises many types; callers want one
            raise PowerError(f"Home Assistant call failed: {e}") from None

    def set(self, on):
        entity = self.cfg["ha_entity"]
        self._request(f"/api/services/{entity.split('.', 1)[0]}/{'turn_on' if on else 'turn_off'}",
                      {"entity_id": entity})

    def state(self):
        """(state, power, unit): state is "on", "off" or another string HA reports."""
        state = self._request(f"/api/states/{self.cfg['ha_entity']}")["state"]
        power = unit = None
        if self.cfg["ha_power_sensor"]:
            sensor = self._request(f"/api/states/{self.cfg['ha_power_sensor']}")
            power, unit = sensor["state"], sensor["attributes"].get("unit_of_measurement")
        return state, power, unit


class Command:
    """power_command is run with one more argument: on, off or state.

    on/off: exit 0 when done, anything else is a failure (stderr is shown).
    state:  print "on" or "off", optionally followed by a power reading ("on 11.5 W"); a non-zero exit
            or other output means the state is unknown, which is fine for scripts that can't tell.
    The command runs as the dcmgr user with DCMGR_ACTION and DCMGR_CONSOLE set."""

    name = "power command"

    def __init__(self, cfg):
        cmd = cfg["power_command"]
        if isinstance(cmd, list):
            self.argv = list(cmd)
        else:  # Windows paths have backslashes, so non-POSIX splitting there, which keeps the quotes
            self.argv = [a[1:-1] if len(a) > 1 and a[0] == a[-1] and a[0] in "\"'" else a
                         for a in shlex.split(cmd, posix=os.name != "nt")]
        self.cfg = cfg

    def _run(self, action, check=True):
        env = dict(os.environ, DCMGR_ACTION=action, DCMGR_CONSOLE=self.cfg["console_id"])
        try:
            r = subprocess.run(self.argv + [action], capture_output=True, text=True, env=env,
                               timeout=self.cfg["power_command_timeout"], stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise PowerError(f"power command `{' '.join(self.argv)} {action}` failed: {e}") from None
        if check and r.returncode:
            why = (r.stderr.strip().splitlines() or [f"exit code {r.returncode}"])[-1]
            raise PowerError(f"power command `{' '.join(self.argv)} {action}` failed: {why}")
        return r

    def set(self, on):
        self._run("on" if on else "off")

    def state(self):
        r = self._run("state", check=False)
        words = r.stdout.split()
        if r.returncode or not words or words[0].lower() not in ("on", "off"):
            return "unknown", None, None
        return words[0].lower(), (words[1] if len(words) > 1 else None), (words[2] if len(words) > 2 else None)


def make_power(cfg):
    """The configured power control, or None. power_command wins over Home Assistant."""
    if cfg["power_command"]:
        return Command(cfg)
    if cfg["ha_url"] and cfg["ha_token"] and cfg["ha_entity"]:
        return HomeAssistant(cfg)
    return None
