# V50 broadcast CAN (passive) — reference

This is a **different layer** from the rest of the toolkit. Everything else here
reads the ECUs on demand with the request/response **A6 diagnostic** protocol
(`0x0FFFFE` → `0x0040xxxx`). The IDs below are the **periodic broadcast** frames
the modules put on the 500k high-speed bus on their own — the traffic a passive
sniffer (SocketCAN, or an ELM in monitor mode) sees without asking.

Source: passive SavvyCAN captures of a real **V50 (P1 — the same car as ours)**,
[damienmaguire/Volvo-V50-CAN-Info](https://github.com/damienmaguire/Volvo-V50-CAN-Info)
(`CANLogs/*.csv`, and the author's `ecm_msgs_full.ftl` filter). Confirmed against
those logs 2026-08-02. **The captures contain no diagnostic traffic**, so they add
nothing to our A6 parameter/DTC/config work — they are only a map of the
broadcast layer, useful if we add passive listening or want to decode broadcast
RPM/speed/etc.

29-bit IDs seen at idle, by frequency (`vary` = how many of the 8 data bytes
change over the capture — a rough "carries live data" hint):

Labels below come from **Chuck3CZ/Volvo-P1-V50-S40-C30-C70-CAN-bus**'s
`volvo_p1_hs_500k.dbc`, decoded from the *same* damienmaguire captures; `msgs` is
this capture's count. `—` = still unlabelled.

| CAN id | msgs | signal (Chuck3CZ HS DBC) |
| --- | ---: | --- |
| `0100082C` | 2247 | **Dashboard** — brightness (B0 low nibble), light sensor (B1 bit7) |
| `0110483C` | 2247 | **PedalSensors** (accel + clutch) |
| `0340412E` | 1565 | **SteeringWheelSensors** |
| `0300410E` | 1549 | **BrakeThrottle** |
| `19000026` | 1044 | **SteeringWheelButtons** |
| `02104136` |  996 | **PSP_SpeedCmd** (power steering) |
| `01C04026` |  996 | — (static / heartbeat) |
| `02000026` |  996 | — |
| `02804026` |  996 | — |
| `02C00020` |  996 | — |
| `00800006` |  775 | **ECM_EngineData** — `RPM = B7·40 − 4400` |
| `0090411E` |  775 | **ECM_Msg3** |
| `03800006` |  775 | **ECM_Msg2** |
| `19E00006` |  310 | **Ignition** |
| `1B200002` |  132 | **PSP_Status** |
| `1AE0092C` |   54 | **PSP_KeepAlive** |
| `19A00002` |  498 | — |
| `1B700030` |  107 | — |
| `1B600002` |   92 | — |
| `1BA0493C` |   90 | — |
| `1A200020` |   72 | — |
| `1BE0493C` |   55 | — |

Cross-checked against our copies of the logs: the `00800006`.B7 RPM signal does
track engine state (raw 110 → 0 rpm; it climbs while revving), though the exact
scale is worth confirming on the car (our rev capture peaked lower than expected).
Brightness/light-sensor bits sit constant in our idle/key logs (not exercised).
So: treat the labels as a strong head start, verify each on-car before trusting —
same platform, possibly a different model year.

**Why a batch decode didn't do this for us:** RPM is a *single* byte (`B7`), and
several frames carry rolling counters and packed bitfields, so a "biggest-swinging
byte-pair" scan just finds counters. The reliable route is event-aligned
correlation (replay in **SavvyCAN**, trigger a known change, watch the byte) — or,
now, start from Chuck3CZ's DBCs.

The **125k low-speed** side has its own `volvo_p1_ls_125k.dbc`, with labelled
cabin frames incl. `AcSettings` (CCM — the climate state), `FuelLevel`,
`ClockTime`, `TurnSignals`, `DoorAndLockStatus`, `ExteriorLights`,
`CruiseControlButtons`, and cluster-control frames **`GaugeCluster_Ctrl` (CEM)**,
`Cluster_BeltSign`/`Cluster_SpeedWarn`/`Cluster_IgnStatus` — the last group is the
CEM telling the DIM what to show, directly relevant to [driving the DIM].

A second P1 reference: **johnbutol/CCM-busmaster** ships a BUSMaster CEM
*simulator* (`SimulatedSystems/cem/cem.cpp`) broadcasting a CEM message set with
periods 30–500 ms; e.g. `0x09C050B8` carries backlight as `0x40 | brightness`.
Different P1 car's ids — use for structure, not literal values.

Not pursued further — passive broadcast decode is outside the read-only A6 scope;
the inventory and method are captured here so nothing is lost.
