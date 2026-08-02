# Transports and their capabilities

Three transports exist because they do different jobs — none is simply "best".

| transport (module) | use it for | state |
| --- | --- | --- |
| J2534 (`transport/j2534.py`) | VXDIAG and the logging proxy on Windows | the main, verified path |
| SocketCAN (`transport/socketcan.py`) | CANable / any Linux CAN interface | code exists, untested on the car |
| ELM327 (`transport/vlinker.py`) | a cheap serial/Bluetooth adapter | standard-OBD only today; raw A6 is experimental |

## Capability matrix

| capability | J2534 / VXDIAG | CANable / SocketCAN | ELM327 |
| --- | :---: | :---: | :---: |
| Raw 500k CAN | yes | yes | maybe (clone-dependent) |
| Low-speed 125k over OBD | yes (vendor bus selector) | depends on the OBD wiring | probably not |
| Passive CAN capture | limited | yes | no |
| Runs on Android | no (Windows DLL) | USB (with a host) | Bluetooth (needs a native app or root) |
| Works with VIDA (the proxy) | yes | no | no |
| Precise timestamps | driver-dependent | yes | no |

Read that as: **VXDIAG/J2534** is the only path that speaks to VIDA and reaches
the low-speed bus (it is what everything here was built and verified on);
**SocketCAN** is the clean choice for raw-CAN work and passive capture on Linux;
**ELM327** is the cheap, ubiquitous option whose ceiling is low — see below.

## ELM327 limitations

Even once the experimental raw-A6 mode works, an ELM327 is not equivalent to
J2534 or a CANable:

- **Only the bus on its physical OBD pins.** No hardware pin/bus switching, so in
  practice you get the powertrain CAN the connector exposes.
- **Probably 500k only.** Reaching the 125k cabin bus the way VXDIAG does (a
  vendor J2534 protocol + bus selector) is not something an ELM327 exposes.
- **Limited polling rate.** It is a serial device with per-line overhead; a few
  parameters per second, not the dozens a J2534 link manages. Keep the daily set
  small.
- **No true passive capture.** It answers requests; it is not a bus sniffer.
- **Clone instability.** Cheap clones often lack `ATCAF0`, custom 29-bit headers,
  or drop frames — behaviour varies unit to unit.
- **Firmware may reinterpret raw frames**, and there are **no precise
  timestamps**, so anything timing-sensitive (e.g. the CEM PIN side-channel) is
  out of reach.

Before trusting an ELM327, run the hardware probe — it checks exactly these:

```
volvo-monitor probe --transport elm --port /dev/rfcomm0
```

```
ELM327 version               ELM327 v1.5
AT interface                 OK
29-bit raw CAN               OK
raw mode (CAF0)              OK
custom request id            OK
Expected ECM response        OK
9/10 reads successful
Average round trip           84 ms
Suggested polling rate       ~11 reads/s

verdict: SUITABLE
```

A Bluetooth ELM must be bound to a serial port first (e.g. `rfcomm bind` on
Linux). On Android, classic-Bluetooth SPP is not reachable without root or a
native app — a WiFi or BLE adapter avoids that; see the daily-scanner discussion.

## Which modules answer on which bus

Read from the proxy logs on the reference car: the 500k powertrain CAN reaches
ECM, ABS and the CEM gateway; DIM and the cabin modules answer only on the
low-speed bus, which VXDIAG exposes as J2534 protocol `32772` at 125k with a
vendor bus-selector config. Both were lifted from VIDA sessions and are handled
in `transport/j2534.py`.
