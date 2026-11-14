#!/bin/sh
# Install, upgrade or remove dcmgr. Run as root from an unpacked copy:
#
#   sh deploy/install.sh                 install or upgrade (an existing config is never overwritten)
#   sh deploy/install.sh --with-capture  also install ffmpeg + v4l-utils for the HDMI capture stick
#   sh deploy/install.sh --with-kos-tool also build kos-tool (a loader for dc-load-ip 3.x) into /opt/dcmgr/bin,
#                                        so the web GUI's Calibrate can run its test program itself
#   sh deploy/install.sh --no-packages   don't touch the package manager (python3 3.8+ must be there)
#   sh deploy/install.sh --uninstall     remove the service and /opt/dcmgr; keep config, state and user
#   sh deploy/install.sh --purge         --uninstall, plus /etc/dcmgr, /var/lib/dcmgr and the dcmgr user
#
# Works with systemd or OpenRC, and apk, apt, dnf, zypper or pacman. ffmpeg is installed automatically on
# upgrade when the existing config uses a capture device.
set -eu
SRC=$(cd "$(dirname "$0")/.." && pwd)
CONF=/etc/dcmgr/dcmgr.json
APP=/opt/dcmgr
STATE=/var/lib/dcmgr

with_capture=0 packages=1 action=install with_kos_tool=0
# kos-tool (https://github.com/andressbarajas/kos-tool), pinned to the commit dcmgr was tested with
KOS_TOOL_REPO=https://github.com/andressbarajas/kos-tool.git
KOS_TOOL_REV=77f6a1ef33382515c2c62b64197b3cccf25b37d1
for arg in "$@"; do
	case $arg in
	--with-capture) with_capture=1 ;;
	--with-kos-tool) with_kos_tool=1 ;;
	--no-packages) packages=0 ;;
	--uninstall) action=uninstall ;;
	--purge) action=purge ;;
	-h | --help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
	*) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
	esac
done

say() { printf '%s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

[ "$(id -u)" = 0 ] || die "run as root (e.g. sudo sh $0, or doas sh $0)"

if [ -d /run/systemd/system ]; then
	init=systemd
elif have openrc-run || [ -x /sbin/openrc-run ]; then
	init=openrc
else
	init=none
fi

# --- uninstall ----------------------------------------------------------------------------------------
if [ "$action" != install ]; then
	case $init in
	systemd)
		systemctl disable --now dcmgr 2>/dev/null || true
		rm -f /etc/systemd/system/dcmgr.service
		systemctl daemon-reload ;;
	openrc)
		rc-service dcmgr stop 2>/dev/null || true
		rc-update del dcmgr default 2>/dev/null || true
		rm -f /etc/init.d/dcmgr ;;
	esac
	rm -rf "$APP"
	if [ "$action" = purge ]; then
		rm -rf /etc/dcmgr "$STATE" /var/log/dcmgr.log /etc/sysctl.d/dcmgr.conf
		if have userdel; then userdel dcmgr 2>/dev/null || true; else deluser dcmgr 2>/dev/null || true; fi
		if have groupdel; then groupdel dcmgr 2>/dev/null || true; else delgroup dcmgr 2>/dev/null || true; fi
		say "dcmgr removed, including its config, state and user"
	else
		say "dcmgr removed; config ($CONF), state ($STATE) and the dcmgr user were kept (--purge removes them)"
	fi
	exit 0
fi

# --- packages -----------------------------------------------------------------------------------------
# an existing config that uses the capture stick needs ffmpeg after an upgrade too
if [ -f "$CONF" ] && grep -Eq '"capture_(device|audio_device)"[[:space:]]*:[[:space:]]*"[^"]' "$CONF"; then
	with_capture=1
fi
pkg_install() {  # pkg_install <required: 0|1> <packages...>
	required=$1; shift
	ok=0
	if have apk; then apk add --no-cache "$@" >/dev/null && ok=1
	elif have apt-get; then
		{ [ -n "${apt_updated:-}" ] || apt-get update -qq; } && apt_updated=1 &&
			DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$@" >/dev/null && ok=1
	elif have dnf; then dnf install -y -q "$@" >/dev/null && ok=1
	elif have zypper; then zypper -n -q install "$@" >/dev/null && ok=1
	elif have pacman; then pacman -S --needed --noconfirm "$@" >/dev/null && ok=1
	else say "no known package manager; install these yourself: $*"; ok=1
	fi
	if [ $ok = 0 ]; then
		[ "$required" = 1 ] && die "could not install: $* (install them yourself, then rerun with --no-packages)"
		say "warning: could not install $*; screen capture won't work until they are installed"
	fi
}
if [ $packages = 1 ]; then
	if have pacman; then pkg_install 1 python; else pkg_install 1 python3; fi
	# Fedora ships ffmpeg as ffmpeg-free; plain ffmpeg needs RPM Fusion
	if [ $with_capture = 1 ]; then
		if have dnf; then pkg_install 0 ffmpeg-free v4l-utils; else pkg_install 0 ffmpeg v4l-utils; fi
	fi
fi

PY=$(command -v python3 || true)
[ -n "$PY" ] || die "python3 not found"
"$PY" -c 'import sys, sqlite3; sys.exit(sys.version_info < (3, 8))' 2>/dev/null ||
	die "$PY must be Python 3.8+ with the sqlite3 module ($("$PY" --version 2>&1))"

# --- user and files -----------------------------------------------------------------------------------
nologin=$(command -v nologin || echo /sbin/nologin)
if ! getent group dcmgr >/dev/null; then
	if have groupadd; then groupadd --system dcmgr; else addgroup -S dcmgr; fi
fi
if ! id dcmgr >/dev/null 2>&1; then
	if have useradd; then
		useradd --system --gid dcmgr --home-dir "$STATE" --no-create-home --shell "$nologin" dcmgr
	else
		adduser -S -D -H -h "$STATE" -s "$nologin" -G dcmgr dcmgr
	fi
fi
# capture stick access (/dev/video*, /dev/snd/*). On a Proxmox LXC pass the devices in with the
# container's gid for these groups, e.g. `pct set <id> -dev0 /dev/video0,gid=<video gid>`.
for g in video audio; do
	getent group $g >/dev/null || continue
	if have usermod; then usermod -aG $g dcmgr; else addgroup dcmgr $g 2>/dev/null || true; fi
done

install -d -m 755 "$APP" "$APP/web"
install -m 644 "$SRC/dcmgr_server.py" "$SRC/dcm.py" "$APP/"
rm -rf "$APP/dcmgr" "$APP/__pycache__"  # no stale modules from an older version
install -d -m 755 "$APP/dcmgr"
install -m 644 "$SRC"/dcmgr/*.py "$APP/dcmgr/"
install -m 644 "$SRC"/web/*.html "$APP/web/"
if [ -f "$SRC/tools/calibrate/calib.elf" ]; then  # the web GUI's Calibrate test program
	install -d -m 755 "$APP/tools/calibrate"
	install -m 644 "$SRC/tools/calibrate/calib.elf" "$APP/tools/calibrate/"
fi
for f in README.md CHANGELOG.md LICENSE; do
	if [ -f "$SRC/$f" ]; then install -m 644 "$SRC/$f" "$APP/"; fi
done
install -d -m 750 -g dcmgr /etc/dcmgr
install -d -m 750 -o dcmgr -g dcmgr "$STATE"

if [ ! -f "$CONF" ]; then
	subnet=$(ip route 2>/dev/null | awk '/scope link/ && !/default/ {print $1; exit}')
	[ -n "$subnet" ] || subnet=192.168.1.0/24
	say "Scanning $subnet for dcload-ip..."
	dcip=$("$PY" "$APP/dcmgr_server.py" --scan "$subnet" | awk 'NR==1 {print $1}') || true
	if [ -n "$dcip" ]; then say "Found dcload at $dcip"; else say "dcload not answering (DC busy or off?)"; fi
	"$PY" - "$subnet" "${dcip:-}" "$STATE" > "$CONF" <<'PY'
import ipaddress, json, secrets, sys
subnet, dcip, state = sys.argv[1:4]
# only non-default settings, so future default changes still apply (see DEFAULTS in dcmgr/common.py)
cfg = dict(subnet=subnet, api_token=secrets.token_urlsafe(18), state_file=f"{state}/state.json",
           extra_ports=[80], ha_url="", ha_token="", ha_entity="switch.dreamcast",
           guide_notes_file="/etc/dcmgr/notes.md")
if dcip:
    # only trust a MAC that belongs to a host which actually answered as dcload;
    # a Sega OUI proves nothing (VMs and W5500 clones use arbitrary MACs)
    try:
        arp = dict(l.split()[0:4:3] for l in open("/proc/net/arp").read().splitlines()[1:])
    except OSError:
        arp = {}
    cfg["dc_ip"], cfg["dc_mac"] = dcip, arp.get(dcip, "")
else:
    # never leave the defaults' placeholder pointing outside this network
    cfg["dc_ip"] = str(next(ipaddress.ip_network(subnet, strict=False).hosts()))
    print("DC not found; set dc_ip/dc_mac in the config", file=sys.stderr)
print(json.dumps(cfg, indent=2))
PY
	chown root:dcmgr "$CONF"
	chmod 640 "$CONF"
	say "Wrote $CONF"
fi
# site notes for the agent guide: group-writable so dcmgr-group members can edit without root
[ -f /etc/dcmgr/notes.md ] || : > /etc/dcmgr/notes.md
chown root:dcmgr /etc/dcmgr/notes.md
chmod 664 /etc/dcmgr/notes.md

# --- optional: kos-tool, so the server can load programs itself (web GUI: Calibrate) --------------------
if [ $with_kos_tool = 1 ]; then
	if [ $packages = 1 ]; then
		if have apk; then pkg_install 1 git build-base elfutils-dev linux-headers pkgconf
		elif have apt-get; then pkg_install 1 git build-essential pkg-config libelf-dev
		elif have dnf; then pkg_install 1 git gcc make elfutils-libelf-devel pkgconf
		elif have zypper; then pkg_install 1 git gcc make libelf-devel pkg-config
		elif have pacman; then pkg_install 1 git base-devel libelf
		fi
	fi
	say "Building kos-tool ${KOS_TOOL_REV%"${KOS_TOOL_REV#???????}"} from $KOS_TOOL_REPO ..."
	build=$APP/src/kos-tool
	rm -rf "$build" && install -d -m 755 "$build"
	if (
		cd "$build"
		git init -q
		git fetch -q --depth 1 "$KOS_TOOL_REPO" "$KOS_TOOL_REV"
		git checkout -q FETCH_HEAD
		# the host tool only; no firmware is embedded, so it can never reflash the DC
		make -C host ROOT="$build" >"$build/build.log" 2>&1
	); then
		install -d -m 755 "$APP/bin"
		install -m 755 "$build/host/build/kos-tool" "$APP/bin/kos-tool"
		say "Installed $APP/bin/kos-tool ($("$APP/bin/kos-tool" -h 2>&1 | grep -m1 . || true))"
		# point the web GUI's Calibrate at it, unless the config already names a loader
		"$PY" - "$CONF" "$APP/bin/kos-tool" <<'PY'
import json, sys
conf, tool = sys.argv[1:3]
cfg = json.load(open(conf))
if cfg.get("loader_command"):
    print(f"{conf} already has a loader_command; left as is")
else:
    cfg["loader_command"] = [tool, "-t", "{ip}", "-x", "{file}"]
    with open(conf, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print(f"Added loader_command to {conf}")
PY
	else
		# don't stop the install over an optional extra: the service still gets updated below
		tail -15 "$build/build.log" 2>/dev/null || true
		say "warning: kos-tool did not build (see $build/build.log); Calibrate's one-button run stays unavailable"
	fi
fi

# --- service ------------------------------------------------------------------------------------------
case $init in
systemd)
	sed "s#/usr/bin/python3 #$PY #" "$SRC/deploy/dcmgr.service" > /etc/systemd/system/dcmgr.service
	chmod 644 /etc/systemd/system/dcmgr.service
	systemctl daemon-reload
	systemctl enable dcmgr >/dev/null 2>&1
	systemctl restart dcmgr
	logs="journalctl -u dcmgr" restart="systemctl restart dcmgr" ;;
openrc)
	sed "s#^command=\"/usr/bin/python3\"#command=\"$PY\"#" "$SRC/deploy/dcmgr.initd" > /etc/init.d/dcmgr
	chmod 755 /etc/init.d/dcmgr
	rc-update add dcmgr default >/dev/null
	rc-service dcmgr restart >/dev/null
	logs="tail /var/log/dcmgr.log" restart="rc-service dcmgr restart" ;;
*)
	say "No systemd or OpenRC found; start it yourself as the dcmgr user:"
	say "  $PY $APP/dcmgr_server.py -c $CONF"
	say "(ports below 1024 such as 80 then need CAP_NET_BIND_SERVICE or net.ipv4.ip_unprivileged_port_start)"
	exit 0 ;;
esac

# --- check it came up ---------------------------------------------------------------------------------
"$PY" - "$CONF" "$logs" <<'PY' || exit 1
import json, sys, time, urllib.request
conf, logs = sys.argv[1:3]
cfg = json.load(open(conf))
port = cfg.get("port", 8731)
host = cfg.get("listen", "0.0.0.0")
host = "127.0.0.1" if host in ("", "0.0.0.0") else host
deadline = time.time() + 20
while True:
    try:
        st = json.load(urllib.request.urlopen(f"http://{host}:{port}/status", timeout=3))
        break
    except Exception as e:
        if time.time() > deadline:
            sys.exit(f"dcmgr did not answer on port {port}: {e}\nSee: {logs}")
        time.sleep(1)
dc = st["dc"]
print(f"dcmgr {st.get('version', '?')} is running on port {port}: DC {dc['state']} at {dc['ip']}")
for p in cfg.get("extra_ports", []):
    try:
        urllib.request.urlopen(f"http://{host}:{p}/status", timeout=3)
        print(f"also serving on port {p}")
    except Exception as e:
        print(f"warning: not serving on port {p} ({e}); see: {logs}")
print(f"API token for clients: {cfg.get('api_token') or '(none)'}")
if not (cfg.get("ha_url") and cfg.get("ha_token")):
    print(f"Add Home Assistant details (ha_url, ha_token, ha_entity) to {conf} to enable power-cycling.")
PY
say "Logs: $logs. After editing $CONF: $restart"
