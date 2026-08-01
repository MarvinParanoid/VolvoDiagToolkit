#!/usr/bin/env sh
# Bind a VMware bridged network to one host interface, deterministically.
#
#   ./scripts/vmware-bridge.sh --show                     # no root needed
#   sudo ./scripts/vmware-bridge.sh enp8s0f3u2
#   sudo ./scripts/vmware-bridge.sh enp8s0f3u2 --single-mac
#   sudo ./scripts/vmware-bridge.sh --auto                # back to automatic
#
# Why this exists: a VXDIAG VCX is not a USB serial gadget, it is a USB
# Ethernet bridge in front of a controller that runs its own DHCP server, and
# the vendor DLL talks to it over IP. On a Linux host the kernel's r8152
# claims the device, so VMware cannot pass the USB device through at all
# ("The connection for the USB device ... was unsuccessful"). Passing it
# through would need an RTL8152 driver in the guest, which Windows 7 does not
# ship. Bridging a virtual NIC onto the host interface is the way in.
#
# VMware's vmnet0 defaults to *automatic* bridging and picks whichever
# interface it likes - on a laptop that is Wi-Fi. Writing
# answer VNET_0_INTERFACE to /etc/vmware/networking is supposed to pin it, but
# whether vmware-networks acts on it varies between builds, so this drives
# vmnet-bridge directly: kill the bridge for that vnet and start it with
# -i <interface>. The config line is written too, in case the installed
# version does honour it on the next full restart.
#
# --single-mac uses vmnet-bridge -1, which makes every guest frame leave with
# the host interface's MAC. Needed whenever the far side accepts one MAC only
# - Wi-Fi, and embedded gadgets like this one that hand out a single DHCP
# lease. Costs nothing to try when plain bridging stays silent.

set -eu

CONF=/etc/vmware/networking
BRIDGE_BIN=/usr/bin/vmnet-bridge
VNET=${VNET:-0}
IFACE=""
SINGLE_MAC=no
MODE=set

for arg in "$@"; do
    case "$arg" in
        --show)       MODE=show ;;
        --auto)       MODE=auto ;;
        --single-mac) SINGLE_MAC=yes ;;
        --force)      FORCE=1 ;;
        -*)           echo "unknown option: $arg" >&2; exit 2 ;;
        *)            IFACE=$arg ;;
    esac
done
FORCE=${FORCE:-0}

show_state() {
    echo "bridge processes:"
    pgrep -af vmnet-bridge | grep -v pgrep || echo "  none running"
    echo
    echo "what the kernel module says (the ground truth):"
    found=no
    for f in /proc/vmnet/bridge*; do
        [ -e "$f" ] || continue
        found=yes
        printf '  %-22s %s\n' "$(basename "$f")" "$(cat "$f" 2>/dev/null || echo '(needs root)')"
    done
    [ "$found" = yes ] || echo "  no bridge attached"
    echo
    echo "$CONF:"
    grep -E "VNET_[0-9]+_INTERFACE" "$CONF" 2>/dev/null | sed 's/^/  /' ||
        echo "  no interface pinned (automatic bridging)"
    echo
    echo "candidate interfaces:"
    for path in /sys/class/net/*; do
        name=$(basename "$path")
        [ "$name" = "lo" ] && continue
        case "$name" in vmnet*|virbr*|docker*|br-*) continue ;; esac
        addr=$(ip -4 -br addr show dev "$name" 2>/dev/null | awk '{print $3}')
        printf '  %-16s %-6s %s\n' "$name" "$(cat "$path/operstate" 2>/dev/null)" \
               "${addr:-no address}"
    done
}

if [ "$MODE" = show ]; then
    show_state
    exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "This has to run as root: it restarts VMware's bridge process." >&2
    echo "  sudo $0 $*" >&2
    exit 1
fi

if [ "$MODE" = set ] && [ -z "$IFACE" ]; then
    echo "usage: sudo $0 <host-interface> [--single-mac]" >&2
    echo >&2
    show_state >&2
    exit 2
fi
if [ -n "$IFACE" ] && [ ! -d "/sys/class/net/$IFACE" ]; then
    echo "no such interface: $IFACE" >&2
    exit 1
fi
[ -x "$BRIDGE_BIN" ] || { echo "$BRIDGE_BIN not found" >&2; exit 1; }

# Rebinding the bridge blips the adapter of any VM using this vnet. Harmless,
# but not something to do behind someone's back mid-session.
if [ "$FORCE" -ne 1 ] && pgrep -f vmware-vmx >/dev/null 2>&1; then
    echo "A virtual machine is running; its adapter on vmnet$VNET will drop when" >&2
    echo "the bridge restarts. Shut it down, or pass --force." >&2
    exit 1
fi

# 1. Persist the choice, in case this VMware build does read it.
if [ -f "$CONF" ]; then
    BACKUP="$CONF.bak.$(date +%Y%m%d-%H%M%S)"
    cp -p "$CONF" "$BACKUP"
    TMP=$(mktemp)
    grep -v "^answer VNET_${VNET}_INTERFACE " "$CONF" > "$TMP" || true
    [ "$MODE" = set ] && echo "answer VNET_${VNET}_INTERFACE $IFACE" >> "$TMP"
    cat "$TMP" > "$CONF"
    rm -f "$TMP"
    echo "config updated (backup: $BACKUP)"
fi

# 2. Restart just this vnet's bridge, with the interface named explicitly.
PIDFILE=/var/run/vmnet-bridge-$VNET.pid
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
else
    pkill -f "vmnet-bridge.* -n $VNET" 2>/dev/null || true
fi
sleep 1

if [ "$MODE" = auto ]; then
    "$BRIDGE_BIN" -d "$PIDFILE" -n "$VNET"
    echo "vmnet$VNET is back to automatic bridging"
else
    if [ "$SINGLE_MAC" = yes ]; then
        "$BRIDGE_BIN" -d "$PIDFILE" -n "$VNET" -i"$IFACE" -1"$IFACE"
        echo "vmnet$VNET bridged to $IFACE (single-MAC)"
    else
        "$BRIDGE_BIN" -d "$PIDFILE" -n "$VNET" -i"$IFACE"
        echo "vmnet$VNET bridged to $IFACE"
    fi
fi
sleep 1

echo
show_state

if [ "$MODE" = set ]; then
    echo
    cat <<EOF
Start the VM and check the adapter on vmnet$VNET from inside the guest.
For a VXDIAG the guest should get an address from the adapter's own DHCP
server; if it comes up with 169.254.x.x instead, nothing reached the DHCP
server - retry with --single-mac before looking anywhere else.
EOF
fi
