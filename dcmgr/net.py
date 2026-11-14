"""Finding dcload on the network: VERS sweeps, ping and ARP lookups."""
import os
import re
import socket
import subprocess
import sys
import time

DCLOAD_PORT = 31313
# dcload-ip command packet: 4-byte id, u32 address, u32 size, payload
VERS_PKT = b"VERS" + bytes(8)

def _udp_socket():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Windows surfaces ICMP port-unreachable as an error on the next recv; disable that.
    if hasattr(socket, "SIO_UDP_CONNRESET"):
        s.ioctl(socket.SIO_UDP_CONNRESET, False)
    return s


def vers_sweep(targets, timeout=1.5, rounds=2):
    """Send dcload's VERS command to every target; return {ip: version} for responders.

    Multiple rounds give the OS time to resolve ARP for hosts it hasn't talked to yet.
    """
    targets = [str(t) for t in targets]
    found = {}
    with _udp_socket() as s:
        s.settimeout(0.1)
        for _ in range(rounds):
            for ip in targets:
                if ip not in found:
                    try:
                        s.sendto(VERS_PKT, (ip, DCLOAD_PORT))
                    except OSError:
                        pass
            deadline = time.monotonic() + timeout / rounds
            while time.monotonic() < deadline and len(found) < len(targets):
                try:
                    data, (ip, _) = s.recvfrom(2048)
                except OSError:
                    continue
                if data[:4] == b"VERS":
                    ver = data[12:].split(b"\0", 1)[0].decode("ascii", "replace").strip()
                    found[ip] = ver or "dcload-ip"
            if len(found) == len(targets):
                break
    return found


def ping(ip, timeout=1.0):
    if os.name == "nt":
        cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), ip]
    elif sys.platform == "darwin":
        cmd = ["ping", "-c", "1", "-t", str(max(1, round(timeout))), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, round(timeout))), ip]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout + 3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    # Windows exits 0 on "Destination host unreachable", so look for an actual reply.
    return b"ttl=" in out.lower()


MAC_RE = re.compile(r"([0-9a-fA-F]{1,2}(?:[:-][0-9a-fA-F]{1,2}){5})")
IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def norm_mac(mac):
    return ":".join(p.zfill(2) for p in re.split("[:-]", mac.strip().lower()))


def arp_lookup(mac):
    """Find the IP currently bound to `mac` in this host's ARP/neighbour table."""
    mac = norm_mac(mac)
    try:
        if os.path.exists("/proc/net/arp"):
            with open("/proc/net/arp") as f:
                text = f.read()
        else:
            text = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in text.splitlines():
        m, i = MAC_RE.search(line), IP_RE.search(line)
        if m and i and norm_mac(m.group(1)) == mac:
            return i.group(1)
    return None
