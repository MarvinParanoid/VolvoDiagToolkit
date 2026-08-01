#!/usr/bin/env sh
# Pin a VMware bridged network to one host interface.
#
#   sudo ./scripts/vmware-bridge.sh enp8s0f3u2        # bridge vmnet0 to it
#   sudo ./scripts/vmware-bridge.sh --show            # what is bridged now
#   sudo ./scripts/vmware-bridge.sh --unpin           # back to automatic
#
# Why this exists: a VXDIAG VCX is not a USB serial gadget, it is a USB
# Ethernet bridge in front of a controller that runs a DHCP server. On a Linux
# host the kernel's r8152 driver claims it, gives the host an address on the
# adapter's private subnet, and VMware then cannot pass the USB device through
# at all ("The connection for the USB device ... was unsuccessful").
#
# Passing it through is the wrong fix anyway: the guest would need an RTL8152
# driver, which Windows 7 does not ship. Bridging a virtual NIC onto the host
# interface gives the guest an address on the adapter's subnet directly, and
# the vendor software finds the device over IP as it expects to.
#
# VMware's default vmnet0 is "automatic" bridging, which picks whichever
# interface it likes — usually the wrong one on a laptop with Wi-Fi. This
# writes answer VNET_<n>_INTERFACE to /etc/vmware/networking and restarts
# VMware's networking so the choice sticks.

set -eu

CONF=/etc/vmware/networking
VNET=${VNET:-0}
IFACE=""
MODE=set

for arg in "$@"; do
    case "$arg" in
        --show)  MODE=show ;;
        --unpin) MODE=unpin ;;
        -*)      echo "unknown option: $arg" >&2; exit 2 ;;
        *)       IFACE=$arg ;;
    esac
done

show_state() {
    echo "bridge processes:"
    pgrep -af vmnet-bridge | grep -v pgrep || echo "  none running"
    echo
    echo "$CONF:"
    grep -E "VNET_[0-9]+_INTERFACE" "$CONF" 2>/dev/null || echo "  no interface pinned (automatic bridging)"
    echo
    echo "candidate interfaces:"
    for path in /sys/class/net/*; do
        name=$(basename "$path")
        [ "$name" = "lo" ] && continue
        case "$name" in vmnet*|virbr*|docker*) continue ;; esac
        addr=$(ip -4 -br addr show dev "$name" 2>/dev/null | awk '{print $3}')
        state=$(cat "$path/operstate" 2>/dev/null)
        printf '  %-16s %-8s %s\n' "$name" "$state" "${addr:-no address}"
    done
}

if [ "$MODE" = show ]; then
    show_state
    exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "This has to run as root: it edits $CONF and restarts VMware networking." >&2
    echo "  sudo $0 $*" >&2
    exit 1
fi

if [ "$MODE" = set ]; then
    if [ -z "$IFACE" ]; then
        echo "usage: sudo $0 <host-interface>" >&2
        echo >&2
        show_state >&2
        exit 2
    fi
    if [ ! -d "/sys/class/net/$IFACE" ]; then
        echo "no such interface: $IFACE" >&2
        exit 1
    fi
fi

if [ ! -f "$CONF" ]; then
    echo "$CONF does not exist — is VMware Workstation installed?" >&2
    exit 1
fi

# Restarting VMware networking yanks the virtual NICs out from under every
# running VM. Refuse rather than surprise someone mid-session.
if pgrep -f vmware-vmx >/dev/null 2>&1; then
    echo "A virtual machine is running. Shut it down first — restarting VMware" >&2
    echo "networking would disconnect its adapters." >&2
    exit 1
fi

BACKUP="$CONF.bak.$(date +%Y%m%d-%H%M%S)"
cp -p "$CONF" "$BACKUP"
echo "backed up $CONF -> $BACKUP"

TMP=$(mktemp)
grep -v "^answer VNET_${VNET}_INTERFACE " "$CONF" > "$TMP" || true
if [ "$MODE" = set ]; then
    echo "answer VNET_${VNET}_INTERFACE $IFACE" >> "$TMP"
fi
cat "$TMP" > "$CONF"
rm -f "$TMP"

if [ "$MODE" = set ]; then
    echo "pinned vmnet$VNET to $IFACE"
else
    echo "vmnet$VNET is back to automatic bridging"
fi

echo "restarting VMware networking..."
if command -v vmware-networks >/dev/null 2>&1; then
    vmware-networks --stop >/dev/null 2>&1 || true
    vmware-networks --start
elif [ -x /etc/init.d/vmware ]; then
    /etc/init.d/vmware restart
else
    echo "could not find vmware-networks; reboot or restart VMware services by hand" >&2
    exit 1
fi

echo
show_state
echo
if [ "$MODE" = set ]; then
    cat <<EOF
Next: start the VM. Its adapter on vmnet$VNET is now on $IFACE's segment.
For a VXDIAG that means the guest gets an address from the adapter's own DHCP
server and can reach it directly — check with ipconfig in the guest, and ping
the adapter (the .1 of that subnet).
EOF
fi
