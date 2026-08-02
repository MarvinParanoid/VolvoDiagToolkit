# CEM configuration blocks (and why writing is hard)

The CEM holds the car's programmed configuration. This toolkit reads it; it does
not write it. Everything here was reverse-engineered from VIDA sessions and
CarCom, and cross-checked against a captured read — see
[../definitions/volvo/p1/config-cem.yaml](../definitions/volvo/p1/config-cem.yaml)
and `tests/../test_config.py`.

Three blocks, all read with the `B9` identity service:

| block | what | shape |
| --- | --- | --- |
| `FB` | vehicle identity | short binary header, then CRLF-separated ASCII fields |
| `FC` | car configuration | fixed byte array, one option per byte |
| `C010` | installed modules | bitmap of fitted control units (read via `A6`) |

## `FB` — vehicle identity

Byte 0 is a data-length marker; bytes 1–5 the config document number (BCD); then
the config issue number and CRLF-separated ASCII fields: **VIN**, FYON, type,
chassis, factory code, structure week, package identity and, further along, the
**market code** (e.g. `EU008`). The VIN decodes at its catalogued offset — that
is what pins the decode (verified against a captured `0xFB`).

## `FC` — car configuration

A flat byte array: each catalogued offset is one option, decoded to a label from
CarCom's value map (`Vehicle sub type` = V50, `Doors` = 5 doors, `Gearbox` =
MTX75, `Particle Filter For Diesel` = Yes with ADM, …). 96 of 99 options decode
to coherent values against a captured block.

**Checksum.** Byte 0 of the block is a marker and **byte 1 is an 8-bit additive
checksum** — the sum of the option bytes (from offset 2 on) modulo 256. Verified
on the captured block (`sum(options) & 0xFF` equals the stored byte). Any write
has to recompute it or the module rejects the block.

## Two multi-frame framings

`B9` block responses are multi-frame, and the modules use **two different frame
numberings**: the identity block is first-frame `0x9x` / consecutive `0x1x`; the
low-speed car-config block is first-frame `0x8x` / consecutive `0x08..0x0F`. The
reassembler keys on the response CAN id and the echoed `[commAddr, F9, id]`
rather than the control nibble, so it handles both (see
`protocol/volvo.py::reassemble_block`, tested against real captures of each).

## Backups and diffs

`dump --ecu CEM` saves these blocks verbatim; `diff before.json after.json` shows
the changed bytes and, for `FB`/`FC`, which decoded field or option moved. That
is the read-only way to see exactly what a change did — dump, change it in VIDA,
dump again, diff.

## Why writing is out of scope

Reading is open; writing is gated, and the gate is real:

1. **Security access (`A3`).** The module refuses any write until unlocked. A
   VIDA session shows `A3 02 <code>` → success (`E3 02 00`). The full unlock uses
   a **per-car 6-byte PIN**.
2. **The PIN is not in CarCom.** The security-code tables hold only placeholders
   (`FFFF…`); the real PIN is car-specific. It can be recovered — but only by a
   CAN-timing side-channel needing dedicated hardware (a Teensy on the bus), not
   a diagnostic adapter like the VXDIAG, whose USB/firmware jitter swamps the
   sub-microsecond signal. See [vtl/volvo-cem-cracker](https://github.com/vtl/volvo-cem-cracker).
3. **The write service itself** is not in any capture yet — only reads (`B9`/`A6`)
   have been recorded. It would come from capturing VIDA actually changing an
   option.

So the pieces for a write are: the PIN (separate hardware), the checksum (known),
and the write service (an un-captured unknown). The toolkit stops at reading and
backing up; the safe path for an actual change is VIDA/VDASH, which handles the
PIN and the write. If that ever changes, it starts with a capture of a real
config write — deliberately, with a `dump` backup, one byte at a time.
