"""Command line entry point."""
import argparse
import ipaddress
import json
import logging
import os
import re
import sys
import threading
from http.server import ThreadingHTTPServer
from . import __version__
from .common import DEFAULTS, log
from .net import vers_sweep
from .manager import Manager
from .web import Handler

DESCRIPTION = "dcmgr-server: arbitrates exclusive access to one network-attached Dreamcast (dcload-ip)."


def main():
    ap = argparse.ArgumentParser(description=DESCRIPTION)
    ap.add_argument("-c", "--config", default="dcmgr.json")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"dcmgr {__version__}")
    ap.add_argument("--scan", metavar="SUBNET", help="list hosts answering as dcload-ip and exit")
    args = ap.parse_args()
    if args.scan:
        found = vers_sweep(ipaddress.ip_network(args.scan, strict=False).hosts(), timeout=3.0)
        for ip, ver in sorted(found.items(), key=lambda kv: ipaddress.ip_address(kv[0])):
            print(ip, ver)
        sys.exit(0 if found else 1)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = dict(DEFAULTS)
    if os.path.exists(args.config):
        with open(args.config) as f:
            cfg.update(json.load(f))
    else:
        log.warning("config %s not found, using defaults", args.config)
    # keep secrets out of the config file if preferred
    cfg["api_token"] = os.environ.get("DCMGR_API_TOKEN", cfg["api_token"])
    cfg["ha_token"] = os.environ.get("DCMGR_HA_TOKEN", cfg["ha_token"])
    unknown = sorted(set(cfg) - set(DEFAULTS))
    if unknown:
        log.warning("ignoring unknown config keys (typo?): %s", ", ".join(unknown))
    if cfg["ha_url"] and not re.match(r"https?://", cfg["ha_url"]):
        log.error("HA reset disabled: ha_url %r needs an http:// or https:// prefix", cfg["ha_url"])
        cfg["ha_url"] = ""
    if cfg["probe"] not in ("vers", "ping", "none"):
        sys.exit(f"config error: probe must be vers, ping or none, not {cfg['probe']!r}")
    if cfg["power_command"] and cfg["ha_url"]:
        log.warning("power_command is set, so the Home Assistant settings (ha_*) are ignored")
    missing = [] if cfg["power_command"] else [k for k in ("ha_url", "ha_token", "ha_entity") if not cfg[k]]
    if 0 < len(missing) < 3:
        log.warning("HA reset disabled: %s not set", ", ".join(missing))

    Handler.mgr = mgr = Manager(cfg)
    mgr.start()
    srv = ThreadingHTTPServer((cfg["listen"], cfg["port"]), Handler)
    srv.daemon_threads = True
    host = cfg["listen"] if cfg["listen"] not in ("0.0.0.0", "") else "127.0.0.1"
    mgr.recorder.base = f"http://{host}:{srv.server_address[1]}"  # it records this server's own streams
    log.info("dcmgr %s listening on %s:%d (DC expected at %s, power control: %s)", __version__,
             cfg["listen"], cfg["port"], mgr.ip, mgr.power_ctl.name if mgr.power_enabled else "disabled")
    for port in cfg["extra_ports"]:
        try:
            extra = ThreadingHTTPServer((cfg["listen"], port), Handler)
        except OSError as e:
            log.warning("could not also listen on port %d: %s (ports <1024 need "
                        "net.ipv4.ip_unprivileged_port_start lowered, see deploy/install.sh)", port, e)
            continue
        extra.daemon_threads = True
        threading.Thread(target=extra.serve_forever, daemon=True).start()
        log.info("also serving on port %d", port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
