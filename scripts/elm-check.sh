#!/usr/bin/env bash
#
# One-shot Bluetooth-ELM327 check on Linux: bind the adapter to an rfcomm serial
# port and run the Volvo A6 probe, then clean up. The ELM must already be paired
# (bluetoothctl: scan on / pair <MAC> / trust <MAC>) — this script does the rest.
#
# Usage:
#   scripts/elm-check.sh [MAC]              # MAC auto-detected from paired devices if omitted
#   CHANNEL=2 BAUD="115200 38400" scripts/elm-check.sh AA:BB:CC:DD:EE:FF
#
# Needs: bluez (rfcomm), sudo (to bind), python3 + pyserial. Car ignition ON.
set -euo pipefail

PORT="${PORT:-/dev/rfcomm0}"
CHANNEL="${CHANNEL:-1}"
BAUDS="${BAUD:-38400 115200 9600}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MAC="${1:-}"

command -v rfcomm >/dev/null || { echo "rfcomm not found — install bluez"; exit 1; }

# 1. Find the ELM MAC among paired devices if not given.
if [ -z "$MAC" ]; then
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

# 2. pyserial present?
python3 -c "import serial" 2>/dev/null || {
    echo "installing pyserial..."; python3 -m pip install --user pyserial >/dev/null
}

# 3. Bind rfcomm (root), make the port accessible, clean up on exit.
echo "binding $PORT -> $MAC (channel $CHANNEL) ..."
sudo rfcomm release "$PORT" 2>/dev/null || true
sudo rfcomm bind "$PORT" "$MAC" "$CHANNEL"
trap 'sudo rfcomm release "$PORT" 2>/dev/null || true' EXIT
sudo chmod a+rw "$PORT" 2>/dev/null || true
sleep 1

# 4. Probe, trying a few serial bauds (a wrong baud looks like garbage/no reply).
cd "$ROOT"
export PYTHONPATH="python"
for BAUD in $BAUDS; do
    echo
    echo "===== probe @ ${BAUD} baud ====="
    if python3 -m volvo_diag probe --transport elm --port "$PORT" --baud "$BAUD"; then
        echo "adapter looks SUITABLE at ${BAUD} baud."
        exit 0
    fi
done

echo
echo "No baud gave a SUITABLE verdict — see the reports above."
echo "If AT replied but there was NO ECM answer: check ignition is ON (key pos II)."
exit 1
