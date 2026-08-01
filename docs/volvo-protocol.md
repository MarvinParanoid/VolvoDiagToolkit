# Volvo P1 proprietary diagnostic protocol (the "A6" read)

Reverse engineered from a VIDA session captured through the proxy on a
2007 V50 D4164T (Bosch EDC16C31), 2026-08-01. Everything here is observed on
the wire, not guessed.

## The engine does not do legislated OBD

The first surprise: this ECM answers **nothing** on the standard addresses.
Captured and confirmed silent:

* physical ISO15765 `0x7E0` → `0x7E8`
* functional OBD broadcast `0x7DF`
* Volvo's 11-bit `0x726` (VIDA tried `22 F114` here and got no reply)

VIDA reads the engine over its own protocol on raw 29-bit CAN instead. Anything
built on standard UDS/OBD (including the `transport/j2534.py` ISO15765 path) will
read nothing from this engine — use the raw-CAN path below.

## Framing

Raw CAN, 500 kbaud, 29-bit identifiers. One request, one response, no ISO-TP.

**Request** — broadcast to `0x000FFFFE`:

```
CD 11 A6 00 7E 01 00 00
│  │  │  └──┴─ 16-bit identifier (0x007E)
│  │  └─ A6  = read service
│  └─ 11     = bank / group (0x11 = live engine data, 0x50 = identity blocks)
└─ C8 + n    = single-frame marker, n = payload length (here 5)
```

The trailing `01` is sent on every read. Bytes 6–7 are padding.

**Response** — on `0x00400021` (identity-block reads come back on `0x00400003`):

```
CE 11 E6 00 7E 03 F2 00
│  │  │  └──┴─ echoed identifier
│  │  └─ E6  = A6 + 0x40, the positive response (KWP/UDS convention)
│  └─ 11     = echoed group
└─ C8 + n    = marker, n = 6
       └ value = 0x03F2, 16-bit big-endian
```

Match a reply to its request by the echoed `(group, identifier)`; the ECM
broadcasts, so the transport reads every frame and picks out its answer. A
non-`E6` service byte (e.g. `7F`) is a rejection.

The codec is `python/volvo_diag/protocol/volvo.py`; the read loop is
`transport/volvo_ecm.py` (`VolvoEcm` over a `CanLink`). The J2534 raw-CAN link
is `J2534CanLink` in `transport/j2534.py`. Every codec test uses real captured
frames, so a green suite means the bytes match the car.

## Identity blocks (multi-frame)

The ECU part numbers and VIN come from group `0x50` (`CD 50 A6 1A 02 01` …) and
arrive as a multi-frame stream on `0x400003` with a Volvo-specific sequence
counter (first bytes `0x9x`, then `0x1x` incrementing). This framing is not yet
decoded — the live single-frame parameters below did not need it. The raw
identity bytes did read out as ASCII: VIN `YV1MW765292483015`, emission class
`EU008`, and several part numbers.

## Identifiers and formulas (from VIDA's CarCom database)

The nine identifiers we captured on the wire were first matched by value range
against VIDA's screen. Then the exact formulas and units came from VIDA's own
**CarCom** SQL database, which turned guesses into ground truth and added ~30
more parameters we never had to reverse engineer. See
[carcom.md](carcom.md) for how the extraction works.

The database is keyed by ECU variant. Ours is **EcuVariant 486, "ECM DV6b"**
(the D4164T's Bosch EDC16C31) — found by the one variant whose identifiers
matched all nine of our captured ones. A few, with the CarCom formula:

| id | parameter | formula | unit |
| --- | --- | --- | --- |
| `05` | engine coolant temperature | (x−2731.4)/10 | °C |
| `2D` | engine speed | x | rpm |
| `2E` | mass air flow | x/10 | kg/h |
| `3A` | intake manifold pressure (boost actual) | x | hPa |
| `63` | fuel rail pressure | x×100 | hPa |
| `7E` | boost pressure, desired | x | hPa |
| `A7` | exhaust / DPF temperature | (x−2731.4)/10 | °C |
| `AE` | DPF differential pressure | x | hPa |
| `B1` | DPF temperature sensor | (x−2731.4)/10 | °C |

The formulas validated exactly against our captures: `05` raw 2934 → 20.26 °C
(VIDA screen 20.26), `A7` raw 3731 → 99.96 °C (screen 99.96), `63` raw 4277 →
427 bar. The full set is in `definitions/volvo/p1/d4164t.yaml`, marked
`verified-against-vida` where we also saw it on the wire, `verified` where it
comes from the database for the matched variant.

## Still to find

`dpf_soot_load`, `regeneration_active` and `distance_since_regeneration` are
not in CarCom's live-data (REID) list for this variant. They are likely
computed values VIDA reads through a routine or DTC block, or shows only on the
DPF status screen. Capture that screen through the proxy, or dig the routine
blocks out of CarCom (they sit under different block types than REID).
