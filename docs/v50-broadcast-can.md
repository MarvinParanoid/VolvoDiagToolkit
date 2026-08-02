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

| CAN id | msgs | vary | label |
| --- | ---: | ---: | --- |
| `0100082C` | 2247 | 6 | panel brightness + night mode (Alfaa123) |
| `0110483C` | 2247 | 7 | — |
| `0340412E` | 1565 | 4 | — |
| `0300410E` | 1549 | 8 | — (powertrain-ish: all bytes move) |
| `19000026` | 1044 | 6 | SWM steering-wheel buttons (Alfaa123) |
| `01C04026` |  996 | 0 | static / heartbeat (no bytes change) |
| `02000026` |  996 | 5 | — |
| `02104136` |  996 | 1 | — |
| `02804026` |  996 | 7 | — |
| `02C00020` |  996 | 4 | — |
| `00800006` |  775 | 8 | — |
| `0090411E` |  775 | 5 | — |
| `03800006` |  775 | 7 | — |
| `19A00002` |  498 | 2 | — |
| `19E00006` |  310 | 5 | ignition status (Alfaa123; byte6 mask 0x40) |
| `1B200002` |  132 | 2 | — |
| `1B700030` |  107 | 6 | — |
| `1B600002` |   92 | 5 | — |
| `1BA0493C` |   90 | 8 | — |
| `1A200020` |   72 | 5 | — |
| `1BE0493C` |   55 | 8 | — |
| `1AE0092C` |   54 | 4 | — |

Only `0100082C`, `19000026`, `19E00006` are labelled — cross-referenced from the
Alfaa123 C30 (P1) gauge project; the rest are unlabelled here. To decode one,
diff byte values within its frames across a known state change (e.g. A/C on↔off,
lights on↔off): the AC/climate state rides inside an existing frame's bytes — no
new id appears when the compressor cycles — so it's a per-byte diff, not an
id-presence diff. See [method.md](method.md) for the general technique.

Not pursued further — passive broadcast decode is out of the current read-only
A6 scope; captured here so the inventory isn't lost.
