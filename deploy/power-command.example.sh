#!/bin/sh
# Example "power_command" for dcmgr: switch the Dreamcast's smart plug directly, without Home Assistant.
# Copy it somewhere the dcmgr user can run it, set PLUG_TYPE and PLUG below, make it executable, then in
# /etc/dcmgr/dcmgr.json:   "power_command": "/etc/dcmgr/power.sh"
#
# dcmgr runs it with one argument:
#   on | off   switch; exit 0 when done, non-zero (with a message on stderr) on failure
#   state      print "on" or "off", optionally followed by a power reading ("on 11.5 W"); exit non-zero
#              if the state can't be read (dcmgr then shows it as unknown)
#
# Written against the plugs' documented local HTTP APIs; adjust for your firmware. Needs wget (BusyBox's
# is fine) or curl.
set -eu
PLUG_TYPE=tasmota          # tasmota (Tasmota firmware) or shelly (Shelly Gen2/Gen3, e.g. Plus Plug S)
PLUG=192.168.1.60          # the plug's address

get() {
	if command -v curl >/dev/null 2>&1; then curl -fsS --max-time 10 "$1"; else wget -qO- -T 10 "$1"; fi
}

case "$PLUG_TYPE:$1" in
tasmota:on) get "http://$PLUG/cm?cmnd=Power%20On" >/dev/null ;;
tasmota:off) get "http://$PLUG/cm?cmnd=Power%20Off" >/dev/null ;;
tasmota:state)
	# {"POWER":"ON"}; plugs with energy monitoring also report Status 8 -> "Power":11
	state=$(get "http://$PLUG/cm?cmnd=Power" | sed -n 's/.*"POWER[0-9]*":"\([A-Z]*\)".*/\1/p' | tr A-Z a-z)
	watts=$(get "http://$PLUG/cm?cmnd=Status%208" 2>/dev/null | sed -n 's/.*"Power":\([0-9.]*\).*/\1/p' || true)
	[ -n "$state" ] || exit 1
	echo "$state${watts:+ $watts W}" ;;
shelly:on) get "http://$PLUG/rpc/Switch.Set?id=0&on=true" >/dev/null ;;
shelly:off) get "http://$PLUG/rpc/Switch.Set?id=0&on=false" >/dev/null ;;
shelly:state)
	# {"id":0, "output":true, "apower":11.5, ...}
	status=$(get "http://$PLUG/rpc/Switch.GetStatus?id=0")
	case $status in
	*'"output":true'*) state=on ;;
	*'"output":false'*) state=off ;;
	*) exit 1 ;;
	esac
	watts=$(printf '%s' "$status" | sed -n 's/.*"apower":\([0-9.]*\).*/\1/p')
	echo "$state${watts:+ $watts W}" ;;
*)
	echo "usage: $0 on|off|state (PLUG_TYPE must be tasmota or shelly)" >&2
	exit 2 ;;
esac
