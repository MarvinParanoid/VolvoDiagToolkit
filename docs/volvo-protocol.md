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

## Identity / configuration (multi-frame)

A module's identity and configuration — VIN, part numbers, software levels,
emission class — is read with the **0xB9** service (`CB 50 B9 FB` for the CEM)
and answered multi-frame on `0x400003`:

* the first frame's high nibble is `0x9` and is a header;
* consecutive frames have high nibble `0x1` and a low-nibble `0..7` sequence
  counter, each carrying seven data bytes after that marker.

Join the consecutive frames' data, and it is CRLF-separated ASCII fields. From
the real car: VIN `YV1MW765292483015`, then part numbers, model id `545`,
emission class `EU008`. `volvo_diag.protocol.volvo.reassemble_identity` /
`identity_fields` do this, validated against the captured frames; the reader is
`VolvoEcm.read_identity` and the CLI command is `volvo-monitor identify`, which
reads it from every Volvo-protocol module (ECM 0x11, CEM 0x50).

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

## Soot load and regeneration: confirmed absent

Searching every block type on all DV6b variants (`scripts/carcom-search.ps1`)
settled it: this ECM **does not publish soot load or distance-since-
regeneration at all** — there is no such parameter in CarCom, which is why
VIDA shows nothing for them on the D4164T. The EDC16C31 simply does not expose
them; later Volvo diesels do. That matches what the owner observed on the car.

Regeneration appears only as:

* write **routines** — `0xA3` start regeneration, `0x81` counter reset, `0xA4`
  drying — actuations, out of scope for a read-only tool;
* a **start/stop autostop-block flag** (`0xB7` bit 4, "blocking autostop,
  cause: DPF regeneration"). When set, a regeneration is running. It is a
  `BLOFF` block, not a `REID` one, so the read may not use the same A6 framing
  — `regeneration_active` in the YAML is a `candidate` until confirmed on the
  car.

So the DPF picture this engine actually gives is: differential pressure
(`0xAE`), filter temperature (`0xA7`/`0xB1`), and the regeneration-in-progress
flag — not a soot percentage.
