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

## Identifiers found (group 0x11)

From an ECM live-data session, each identifier's value range matched against
VIDA's displayed reading. Engine was mostly at rest, so ranges are narrow.

| id | VIDA parameter | raw range | reading | status |
| --- | --- | --- | --- | --- |
| `3A` | intake manifold pressure (boost actual) | 985–1028 | ×0.1 → kPa | experimental |
| `7E` | boost pressure requested | 1010–1215 | ×0.1 → kPa | experimental |
| `AE` | DPF differential pressure | 0–28 | ×0.1 → kPa | experimental |
| `2E` | mass air flow | 0–2710 | ~×0.01 kg/h | discovered |
| `9E` | air mass per stroke | 2347–5186 | ~×0.1 mg | discovered |
| `05` | air mass, expected | ~2935 | ~×0.1 mg | discovered |
| `50` | fuel rail pressure | 0–32767 | scale TBD | discovered |
| `63` | (fuel-pressure regulator current?) | 65–4277 | scale TBD | not yet defined |

The three pressures are anchored to physics — all sat near atmospheric
(~1013 hPa = 101.3 kPa) with the engine off — so their scale is trustworthy.
The rest have the identifier confirmed but the scale not, so they carry no unit
in `definitions/volvo/p1/d4164t.yaml`. Nothing is `verified` until raw and VIDA
are compared at the same instant.

## Still to find

`dpf_soot_load`, `exhaust_temperature`, `regeneration_active` and
`distance_since_regeneration` were not among the identifiers captured in that
session. Two ways to get them:

1. another live-data capture with those readouts open in VIDA (staged tags),
   correlating raw against the displayed value at the same moment;
2. VIDA's CarCom SQL database, which stores the identifier, scale and unit per
   ECU variant directly — no guessing (see the Volvo-VIDA project in the
   research notes).
