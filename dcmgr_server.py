#!/usr/bin/env python3
"""dcmgr-server: arbitrates exclusive access to one network-attached Dreamcast (dcload-ip).

  * exclusive leases kept alive by heartbeats, plus a FIFO wait queue
  * finds the DC when its IP changes (dcload VERS sweep, or ARP lookup by MAC)
  * power-cycles the DC through Home Assistant when dcload stops answering
  * web GUI: lock and power, queue control and pausing, hands-on holds, activity log

Run it on an always-on host on the same LAN segment as the DC. Stdlib only, Python 3.8+.

The code lives in the dcmgr/ package next to this file. This script stays the entry point
(python3 dcmgr_server.py -c dcmgr.json) and re-exports the main names for tests and tools.
"""
import logging  # noqa: F401
import os
import sys
from http.server import ThreadingHTTPServer  # noqa: F401

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dcmgr import __version__  # noqa: E402,F401
from dcmgr.common import DEFAULTS, HERE, LEVELS, ApiError, log  # noqa: E402,F401
from dcmgr.guide import CLIENT_DEFAULTS_LINE, render_guide  # noqa: E402,F401
from dcmgr.manager import Manager  # noqa: E402,F401
from dcmgr.server import main  # noqa: E402
from dcmgr.store import SCHEMA_VERSION, Store  # noqa: E402,F401
from dcmgr.web import Handler  # noqa: E402,F401

if __name__ == "__main__":
    main()
