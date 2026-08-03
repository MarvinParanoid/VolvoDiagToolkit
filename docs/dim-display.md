# Text on the instrument cluster (DIM) — design & status

**Experimental. A write. Not confirmed on P1.** This is the plan for showing live
diagnostics (boost, EGT, DPF temp, regen, voltage) on the factory cluster's text
row — a "diagnostic line" with no phone or extra screen. It is the opposite end
of the read-only core, so it is opt-in and staged.

## How it works (from the P2 reference, Vaizer/DDFE)

Two halves, on two buses:

1. **Read** the values with the normal diagnostic protocol — request/response to
   the ECM (and others) on the 500k bus via `0x0FFFFE` (our existing A6 path).
2. **Write** the text as a **broadcast** on the **125k cabin bus**, spoofing the
   phone/message module. The DIM's text row is the phone display, so you:
   - send screen control to the **LCD id**: enable (`C0 00…05`, `C0 00…00`), clear
     (`E1 FE …`), disable (`00…04`);
   - send the text to the **PHM id** as five 8-byte frames (a 32-char, 2×16
     string) with sequence markers:
     ```
     A7 00 c0..c5 | 21 c6..c12 | 22 c13..c19 | 23 c20..c26 | 65 c27..c31 00 00
     ```
   Clear before each message; ~10–30 ms between frames.

This is raw broadcast injection — not a diagnostic request — which is why it is
gated. Framing and command bytes are in `volvo_diag.volvo.dim` (`encode_text`,
`DimWriter`), unit-tested; the CLI is `volvo-monitor dim-text`.

## Two paths (both P1-confirmed by andrewgabler/VolvoDIM)

There are actually **two** ways to drive the cluster:

**A. Text** — the broadcast-the-phone path above. VolvoDIM documents it exactly,
with the framing confirmed: `A7 00` starts the message, the counter continues
`21`, `22`, `23`…, and the **last frame's marker is `6N`** where N = the number of
text chars in it (our `encode_text` sends a full 32-char message, so its last
marker is `65`). ~30 ms between frames; a trailing space is required or the last
row is dropped. Clear = `E1 FE …`.

**B. Gauges & warning lamps** — via our **normal A6 request/response** to DIM
`0x51` (no broadcast, id known):
```
0FFFFE  CB 51 B2 02 …               # B2 control routine: sweep all instruments
0FFFFE  CE 51 B0 09 01 FF 04 00     # B0 IO-control: ABS lamp on   (…08… = SPIN lamp)
0FFFFE  D8 00 00 00 00 00 00 00     # keep the diagnostic session alive — resend ~every 5 s
```
Path B is cleaner for *state* (lamps, gauge test) and fits the toolkit directly;
path A is for *free text*. Diagnostic-mode only — on your own car.

## The open piece: which id for our year

The framing is fixed; only the two text/control ids vary by model year. Known
values (VolvoDIM + DDFE):

| year | text / PHM id | control / LCD id |
| --- | --- | --- |
| 2001 | `0x00400008` | `0x00C0200E` |
| 2002 | `0x00C00008` | `0x0220200E` |
| facelift (≥2007) | `0x01800008` | `0x02A0240E` |
| MY04 CEM→DIM | — | `0x02A07428` |

Our **V50 is 2007 → facelift**, so the first candidates to try are **PHM
`0x01800008` / LCD `0x02A0240E`** (then the MY04/2002 rows). These broadcast ids
don't show up in the passive captures (text frames only exist during an actual
message), so confirm by trying them on-car with `dim-text` and watching the
cluster. Related: our **BPM/phone module is comm `0x7C`** — the same module family.

Sources: andrewgabler/VolvoDIM (`Research/Notes on CANBUS`, an Arduino library
that powers a DIM standalone), Vaizer/DDFE, and the svxc.se / motor-talk threads
plus S. Visla's 2018 thesis linked from those notes.

## Using the probe (once you have candidate ids)

```sh
# a WRITE — needs --enable-writes. Model-year presets pick the id pair; our V50
# is facelift, so start there:
volvo-monitor --transport j2534 --enable-writes dim-text --year facelift "HELLO"
# nothing on the cluster? try the other presets, or explicit ids:
volvo-monitor --transport j2534 --enable-writes dim-text --year 2002 "HELLO"
volvo-monitor --transport j2534 --enable-writes dim-text --phm-id 0x… --lcd-id 0x… "HELLO"
```

## Staged plan

1. **Find the P1 ids** (capture or probe). Until then the feature is dormant.
2. **Static string PoC** — `dim-text "HELLO"`; confirm the cluster shows it and how
   long it holds before the CEM/message system overwrites it.
3. **Live output** — a small loop reading 1–2 params and refreshing the text at a
   few Hz, with throttling. Likely needs to "hold" the display against the CEM.

## Risks / constraints

- The DIM text row is normally driven by the CEM message system — our text may be
  overwritten, or need continuous refresh to persist.
- Do **not** clobber real driver warnings; keep it a passive info line.
- Short field (2×16 chars) — a couple of values at a time.
- Sustained broadcast adds traffic to the 125k bus.
- Once the P1 ids are confirmed, put them in the vehicle profile so the command
  can default them per car.
