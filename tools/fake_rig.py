#!/usr/bin/env python3
"""Fake Dreamcast + Home Assistant for testing dcmgr without hardware.

The fake dcload answers VERS on 127.0.0.<ip>:31313. The fake HA accepts
switch.turn_off/turn_on; after a power-cycle the DC comes back on the *next*
address in --ips, mimicking the static-IP-falls-back-to-DHCP bug.

Test controls (POST, no auth):
  /_test/freeze    dcload stops answering (program running / crashed)
  /_test/move      DC jumps to the next IP without a reset
  /_test/dead      power-on no longer brings dcload back (dead boot disc); again to undo
"""
import argparse
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeDC:
    def __init__(self, ips, boot_delay):
        self.ips, self.idx, self.boot_delay = ips, 0, boot_delay
        self.powered, self.frozen, self.dead = True, False, False
        self.plug = True  # the switch itself; dcload only answers once booted
        self.calls = []   # HA services called, e.g. ["turn_off", "turn_on"] (for tests)
        self.sock = None
        self.lock = threading.Lock()
        self._bind()

    def _bind(self):
        if self.sock:
            self.sock.close()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.ips[self.idx], 31313))
        threading.Thread(target=self._serve, args=(self.sock,), daemon=True).start()
        print(f"[fake-dc] dcload up at {self.ips[self.idx]}", flush=True)

    def _serve(self, sock):
        while True:
            try:
                data, addr = sock.recvfrom(2048)
            except OSError:
                return
            if data[:4] == b"VERS" and self.powered and not self.frozen:
                sock.sendto(data[:12] + b"DCLOAD-IP 2.0.3-fake\0", addr)

    def close(self):
        with self.lock:
            self.powered = False
            if self.sock:
                self.sock.close()
                self.sock = None

    def move(self):
        with self.lock:
            self.idx = (self.idx + 1) % len(self.ips)
            self._bind()

    def power(self, on):
        self.plug = on
        with self.lock:
            if not on:
                self.powered = False
                print("[fake-dc] power off", flush=True)
                return
        def boot():
            time.sleep(self.boot_delay)
            if self.dead:
                print("[fake-dc] boot failed (dead)", flush=True)
                return
            with self.lock:
                self.idx = (self.idx + 1) % len(self.ips)
                self.frozen, self.powered = False, True
                self._bind()
        threading.Thread(target=boot, daemon=True).start()


def make_ha_server(dc, host="127.0.0.1", port=8123, token="test-ha-token"):
    """Fake Home Assistant (plus the /_test controls) driving `dc`. Port 0 picks a free one."""

    class HA(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            # /api/states/<entity>: the plug's on/off state, or a power sensor's draw
            if not self.path.startswith("/api/states/"):
                self.send_response(404)
                self.end_headers()
                return
            if "power" in self.path:
                st = {"state": "11.5" if dc.plug else "0.0", "attributes": {"unit_of_measurement": "W"}}
            else:
                st = {"state": "on" if dc.plug else "off", "attributes": {}}
            out = json.dumps(st).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path == "/_test/freeze":
                dc.frozen = True
                print("[fake-dc] frozen", flush=True)
            elif self.path == "/_test/dead":
                dc.dead = not dc.dead
                print(f"[fake-dc] dead={dc.dead}", flush=True)
            elif self.path == "/_test/move":
                dc.move()
            elif self.path.startswith("/api/services/"):
                if self.headers.get("Authorization") != f"Bearer {token}":
                    self.send_response(401)
                    self.end_headers()
                    return
                print(f"[fake-ha] {self.path} {body.decode()}", flush=True)
                dc.calls.append(self.path.rsplit("/", 1)[1])
                dc.power(self.path.endswith("/turn_on"))
            else:
                self.send_response(404)
                self.end_headers()
                return
            out = json.dumps([]).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = ThreadingHTTPServer((host, port), HA)
    srv.daemon_threads = True
    return srv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ips", default="127.0.0.20,127.0.0.21")
    ap.add_argument("--ha-port", type=int, default=8123)
    ap.add_argument("--ha-token", default="test-ha-token")
    ap.add_argument("--boot-delay", type=float, default=2)
    args = ap.parse_args()
    dc = FakeDC(args.ips.split(","), args.boot_delay)
    srv = make_ha_server(dc, port=args.ha_port, token=args.ha_token)
    print(f"[fake-ha] listening on 127.0.0.1:{args.ha_port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
