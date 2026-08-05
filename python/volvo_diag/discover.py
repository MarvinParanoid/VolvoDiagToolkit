"""Helpers for hunting unknown Volvo A6 identifiers.

The Volvo A6 read returns raw value bytes with no type info, so when sweeping the
identifier space for an undocumented parameter (e.g. DPF soot grams, differential
pressure, distance-since-regen) you have to eyeball which decode fits. These
functions offer the plausible interpretations and a couple of range hints so a
human can spot the right one. Read-only; pairs with the `discover` CLI sweep.
"""

from __future__ import annotations

# Bosch EDC16 tends to scale packed integers by these divisors — soot mass /100 g,
# ash /10 g, DPF Δp /80 kPa (Ford DV6), etc. — so try them on any 16-bit value.
_DIVISORS = (4, 10, 80, 100)


def interpret(raw: bytes) -> dict:
    """Candidate numeric decodes of a raw A6 value, keyed by a short label. The
    caller prints these next to the raw bytes; none is authoritative."""
    out: dict = {}
    if len(raw) >= 1:
        out["u8"] = raw[0]
    if len(raw) >= 2:
        u16 = int.from_bytes(raw[:2], "big")
        out["u16"] = u16
        for d in _DIVISORS:
            out[f"/{d}"] = round(u16 / d, 3)
        out["temp"] = round(u16 * 0.1 - 273.14, 1)   # temperature shape (0x00A7)
        out["pct"] = round(u16 * 100 / 8192, 2)       # percent shape (EGR 0x002C)
    return out


_ABSENT_BELOW = -250   # a value that decoded this low is a sensor-absent default


def classify(raw: bytes, value) -> str:
    """Coarse triage of a DEFINED parameter's read for the `verify` sweep:

    * ``absent`` — the id answered but with a not-present default: all-0xFF bytes,
      or a temperature that collapsed to ~-273 because its raw was 0 (we saw this
      on oil-temp / an unfitted DPF sensor). The param is defined but the sensor/
      function isn't on this car.
    * ``answered`` — a real-looking value.

    A timeout (``no-answer``) and a decode error are classified by the caller,
    since those don't have a (raw, value) pair."""
    if not raw or all(b == 0xFF for b in raw):
        return "absent"
    # 0x7FFF (max signed 16-bit) is the other common "signal not available"
    # sentinel — the ECM answers but with a rail value (we saw it on lambda).
    if len(raw) == 2 and raw in (b"\x7f\xff", b"\xff\xff"):
        return "absent"
    if (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value <= _ABSENT_BELOW):
        return "absent"
    return "answered"


def hints(raw: bytes) -> list:
    """Short flags when a decode lands in a notable physical range — a nudge for
    the DPF/diesel-health hunt, not a claim. Empty for values that fit nothing."""
    h: list = []
    if len(raw) < 2:
        return h
    u16 = int.from_bytes(raw[:2], "big")
    if u16 == 0 or u16 == 0xFFFF:
        return h                                       # not-present / sensor absent
    if 1 <= u16 <= 65:
        h.append("soot?g")                             # DPF soot grams, 0-65 raw
    if 1 <= u16 / 100 <= 65:
        h.append("soot?/100")
    t = u16 * 0.1 - 273.14
    if 40 <= t <= 900:
        h.append(f"temp?{t:.0f}C")                     # exhaust/DPF temperature
    p = u16 * 100 / 8192
    if 1 <= p <= 100:
        h.append(f"pct?{p:.0f}%")
    return h
