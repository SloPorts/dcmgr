#!/usr/bin/env python3
"""Remove the build machine's details from a KallistiOS ELF before distributing it.

KOS compiles a banner into every program (printed at boot) that includes the build date and
"<user>@<host>:<KOS_BASE>" of whoever built KOS. This overwrites those two lines, in place and at the same
length, with neutral text. Usage: scrub_banner.py calib.elf
"""
import re
import sys


def scrub(data):
    start = data.find(b"KallistiOS v")
    if start < 0:
        raise SystemExit("no KallistiOS banner found")
    end = data.find(b"\0", start)
    banner = data[start:end]
    lines = banner.split(b"\n")
    out = []
    for line in lines:
        body = line[2:] if line.startswith(b"  ") else None
        if body is not None and re.fullmatch(rb"[^@\s]+@[^:\s]+:\S+", body):  # user@host:/path/to/kos
            line = line[:2] + b"build host not recorded".ljust(len(body))[:len(body)]
        elif body is not None and re.search(rb"\b\d{2}:\d{2}:\d{2}\b", body):  # the date(1) line
            line = line[:2] + b"build date not recorded".ljust(len(body))[:len(body)]
        out.append(line)
    new = b"\n".join(out)
    assert len(new) == len(banner)
    return data[:start] + new + data[end:], new != banner


def main():
    path = sys.argv[1]
    with open(path, "rb") as f:
        data = f.read()
    data, changed = scrub(data)
    with open(path, "wb") as f:
        f.write(data)
    print(f"{path}: banner {'scrubbed' if changed else 'already clean'}")


if __name__ == "__main__":
    main()
