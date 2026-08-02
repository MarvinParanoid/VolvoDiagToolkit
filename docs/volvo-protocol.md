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

## Service bytes

The service family is KWP2000-style: the positive response is the request
service **+ 0x40** (`A6→E6`, `B9→F9`, `AE→EE`, `B8→F8`, `A3→E3`). This table is
consolidated from our own captures and corroborated against external P1 dumps
(Alfaa123's `Codes.txt`, Tigo2000's `ECU-Commands.txt`). **This toolkit is
read-only** — only the read services are driven; the rest are listed for
decoding logs and for reference.

| service | resp | meaning | in this toolkit |
| :---: | :---: | --- | --- |
| `A1` | `E1` | keep-alive / tester present | — |
| `A3` | `E3` | security access (send PIN) | reference only |
| `A5`/`A6`/`A7` | `+40` | read data by offset / **identifier** / address | **`A6` = live read** |
| `A8`/`A9` | `+40` | start / stop transmission (periodic) | — |
| `AA` | `EA` | dynamically define data | — |
| `AB`–`AD` | `+40` | freeze-frame data | — |
| `AE` | `EE` | **read DTC** (`AE 1B` list, `AE 31` +status) | **implemented** |
| `AF` | `EF` | **clear DTC** (`AF 11`) — a write | reference only |
| `B0`/`B1` | `+40` | IO control by offset / identifier (actuation) | reference only |
| `B2` | `F2` | control routine | reference only |
| `B4` | `F4` | define read/write ECU data | reference only |
| `B8`/`BA` | `F8`/`FA` | **write** data block by offset / address (config) | reversed, [not driven](#writing-configuration-the-0xb8-service) |
| `B9`/`BB` | `F9`/`FB` | **read** data block by offset / address | **`B9` = identity/config read** |

Constants for these live in `protocol/volvo.py` (`SERVICE_READ`,
`SERVICE_IDENTITY`, `SERVICE_DTC`, and the reference-only `SERVICE_SECURITY`,
`SERVICE_CLEAR_DTC`, `SERVICE_WRITE`).

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

## Reading trouble codes (the 0xAE service)

Reversed from the `22-write-dts` capture (the ECM had a real fault: `2A30`,
clogged particulate filter). Trouble codes use service **`0xAE`**, answered
**`0xEE`**; the byte after the service is a sub-function:

* **`AE 1B`** — list the module's active codes. VIDA sends it to *every* module
  to draw the red/green network map; an empty module answers `EE 1B 00 00`, one
  with a fault answers `EE 1B <code><code>… 00 00` (each code 2 bytes, `0000`
  terminates). The ECM here returned `2A30`.
* **`AE 31`** — a code with its status byte (`EE 31 2A30 11`; `0x11` = confirmed).
* **`AE 70 <code>`** / **`AE 18 <code>`** — freeze-frame and extended data for
  one code (multi-frame).

Codes are Volvo's own 16-bit numbers (4 hex digits), not OBD `Pxxxx`; the text
comes from the CarCom catalogue (`definitions/volvo/p1/dtc-*.yaml`). Implemented
read-only: `VolvoEcm.read_dtcs` (service `AE 1B`) and `volvo-monitor dtc`, which
sweeps every Volvo module and names each code.

**Clearing** codes is a WRITE: **`AF 11`** per module, acknowledged **`EF 11`**
(reversed from VIDA's own clear sweep in the same capture — `CB 11 AF 11` →
`CB 11 EF 11`). It is opt-in, never automatic: `volvo-monitor dtc --clear`, or the
**Clear codes** button in the dashboard's Codes tab (behind a confirm).
`VolvoEcm.clear_dtcs` drives it.

## Writing configuration (the 0xB8 service)

Captured on 2026-08-02 by changing two DIM settings in VIDA through the proxy
(°C→°F and 24h→12h clock) and reverting them, both directions logged. The write
is the mirror of the 0xB9 read:

* **service `0xB8`** (write), acknowledged with `0xF8` (= `0xB8 | 0x40`);
* single-frame request `[C8+len][commAddr][B8][id][value…]`, e.g.
  `CC 51 B8 0A 01` = write DIM (commAddr `0x51`) block id `0x0A` ← `0x01`;
* ack `[C8+3][commAddr][F8][id]` (`CB 51 F8 0A`), then a `0xB9` read-back returns
  the new value.

The two on-screen changes toggled three one-byte blocks `01`↔`02` — DIM `0x0A`,
ICM (`0x54`) `0x22`, DIM `0x10` — plus a constant `DIM 0x4A ← 01` written each
time (looks like a commit/apply). All of it happened on the **125k low-speed
bus** (J2534 protocol `0x8004`); DIM and ICM are only reachable there.

**No security access (`0xA3`) or seed/key preceded the writes** — DIM/ICM
configuration is writable directly. (This is unlike the CEM, whose write path is
still unknown and where a 6-byte PIN is expected; see [carcom.md](carcom.md).)

This toolkit stays read-only: the 0xB8 service is documented here, not
implemented. Enabling a specific, guarded write (e.g. Bluetooth streaming on the
phone module) is a deliberate future step, not a general capability.

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
