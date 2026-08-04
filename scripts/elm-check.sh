#!/usr/bin/env bash
#
# One-shot Bluetooth-ELM327 check on Linux. Connects to the adapter over a
# classic-Bluetooth RFCOMM/SPP socket by MAC and runs the Volvo A6 probe. No
# rfcomm bind, no /dev node, no root: just pair+trust the adapter once, then run.
#
#   bluetoothctl:  scan on / pair <MAC> / trust <MAC>
#
# Usage:
#   scripts/elm-check.sh [MAC]          # MAC auto-detected from paired devices if omitted
#   CHANNEL=2 scripts/elm-check.sh AA:BB:CC:DD:EE:FF
#
# Needs: python3, a paired ELM, car ignition ON (key position II).
set -euo pipefail

# Channels to try. Most SPP ELM adapters sit on 1, but clones vary (2, 3, ...).
# Override with CHANNEL=N to pin a single one.
CHANNELS="${CHANNEL:+$CHANNEL}"
CHANNELS="${CHANNELS:-1 2 3 4 5}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MAC="${1:-}"

# 1. Find the ELM MAC among paired devices if not given.
if [ -z "$MAC" ]; then
    command -v bluetoothctl >/dev/null || { echo "install bluez (bluetoothctl)"; exit 1; }
    MAC="$(bluetoothctl devices 2>/dev/null \
        | grep -iE 'obd|elm|vlink|vgate|v-link|konnwei|icar|viecar' \
        | head -1 | awk '{print $2}')" || true
    [ -z "$MAC" ] && {
        echo "Could not auto-detect a paired ELM. Pair it first:"
        echo "  bluetoothctl -> scan on -> pair <MAC> -> trust <MAC>"
        echo "then: $0 <MAC>"
        exit 1
    }
    echo "using paired device: $MAC"
fi

# 2. Probe over a direct RFCOMM socket (transport reads the MAC as its --port).
#    Walk the candidate channels until one opens; "Connection refused" (Errno 111)
#    just means "not this channel" (or not paired), so keep trying.
cd "$ROOT"
export PYTHONPATH="python"
for ch in $CHANNELS; do
    echo
    echo "===== probe $MAC (channel $ch) ====="
    if python3 -m volvo_diag probe --transport elm --port "${MAC}@${ch}"; then
        echo "adapter looks SUITABLE (channel $ch)."
        exit 0
    fi
done

echo
echo "Not SUITABLE — see the reports above."
echo "  - Every channel 'Connection refused' (Errno 111): the adapter is not"
echo "    accepting RFCOMM. Pair AND trust it, and make sure nothing else holds it:"
echo "      bluetoothctl -> power on -> agent on -> scan on"
echo "      pair $MAC   ->   trust $MAC   ->   (do NOT 'connect')"
echo "    Then re-run. If it still refuses, the adapter may be BLE-only (no SPP)."
echo "  - 'Passive 500k traffic: N frames' but NO ECM answer: the adapter is on"
echo "    the right bus — the A6 request/filter or the gateway is the issue, not"
echo "    wiring. (With the key in II this is the case to dig into.)"
echo "  - 'Passive 500k traffic: none' + no A6 answer: the P1 OBD port is gatewayed"
echo "    (request/response only, no broadcast), so raw-A6 is likely blocked here."
echo "    This is NOT a wiring fault — standard OBD is a separate, working path:"
echo "      python -m volvo_diag --transport elm --port <MAC>@<ch> info"
echo "    (P1 answers standard OBD on 29-bit; that's the cheap-ELM daily path.)"
exit 1
