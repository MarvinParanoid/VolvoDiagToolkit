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

CHANNEL="${CHANNEL:-1}"
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
cd "$ROOT"
export PYTHONPATH="python"
echo
echo "===== probe $MAC (channel $CHANNEL) ====="
if python3 -m volvo_diag probe --transport elm --port "${MAC}@${CHANNEL}"; then
    echo "adapter looks SUITABLE."
    exit 0
fi

echo
echo "Not SUITABLE — see the report above."
echo "  - AT replied but NO ECM answer: check ignition is ON (key pos II)."
echo "  - connect refused/timed out: pair AND trust the adapter in bluetoothctl."
exit 1
