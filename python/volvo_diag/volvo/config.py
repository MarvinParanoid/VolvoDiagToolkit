"""Decoding a module's programmed configuration blocks. Read-only.

Three blocks, each read with a service this toolkit already speaks:

    FB     vehicle identity    VIN, chassis, market  (0xB9 block read)
    FC     car configuration   one option per byte    (0xB9 block read)
    C010   installed modules   which units are fitted (0xA6 read)

The decode maps come from CarCom (VIDA's own database); see
``definitions/volvo/p1/config-cem.yaml``. The identity offsets are verified
against a captured 0xFB response (the VIN falls exactly at its catalogued
offset); the car-config block uses the same fixed-offset scheme on a plain
byte array, so the offsets carry over.

Only the identity block has been checked against a real capture. The car
configuration and installed-modules decode is derived from the same catalogue
but has not yet been confirmed against a car; callers should present it as
such until a 0xFC / 0xC010 capture verifies it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

# The identity block's fixed offsets are exact up to and including "Package
# identity"; past there CarCom's field list is incomplete for this variant, so
# the market code is recovered by pattern instead of by offset.
_IDENTITY_UNTIL = "Package identity"
_MARKET_RE = re.compile(r"^[A-Z]{2}[0-9]{2,}$")


def default_map_path() -> Path:
    """The bundled CEM configuration map."""
    here = Path(__file__).resolve()
    for base in here.parents:
        candidate = base / "definitions" / "volvo" / "p1" / "config-cem.yaml"
        if candidate.exists():
            return candidate
    return Path("definitions/volvo/p1/config-cem.yaml")


def load_map(path: str | Path | None = None, profile_dir: str | Path | None = None) -> dict:
    # Prefer the selected profile's own config map when one is given, so several
    # cars can ship different maps without colliding; otherwise the bundled one.
    if path is None and profile_dir is not None:
        candidate = Path(profile_dir) / "config-cem.yaml"
        if candidate.exists():
            path = candidate
    path = Path(path) if path else default_map_path()
    with Path(path).open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _ascii(segment: bytes) -> str:
    """Printable ASCII of a field, stripping padding and control bytes."""
    text = segment.decode("latin-1")
    return "".join(c for c in text if 32 <= ord(c) < 127).strip()


@dataclass(frozen=True)
class Field:
    name: str
    value: str


def decode_identity(raw: bytes, cmap: dict) -> list:
    """Decodes the 0xFB identity block into (name, value) fields.

    Uses the catalogued fixed offsets for the fields known to be reliable and
    recovers the market code by pattern from the block's CRLF text.
    """
    fields: list = []
    reliable = True
    for entry in (cmap.get("identity") or {}).get("fields", []):
        if not reliable:
            break
        off, ln = int(entry["offset_bits"]), int(entry["len_bits"])
        segment = raw[off // 8: (off + ln) // 8]
        value = _ascii(segment)
        name = entry["name"]
        # skip the binary header fields that carry no readable value
        if name not in ("Data length", "Car configuration document number"):
            fields.append(Field(name, value))
        if name == _IDENTITY_UNTIL:
            reliable = False

    market = _market_code(raw)
    if market:
        fields.append(Field("Market code", market))
    return fields


def _market_code(raw: bytes) -> str:
    """The market code (e.g. 'EU008') recovered from the CRLF text fields."""
    text = raw.decode("latin-1")
    for token in re.split(r"[\r\n\x00]+", text):
        token = token.strip()
        if _MARKET_RE.match(token):
            return token
    return ""


@dataclass(frozen=True)
class Option:
    name: str
    raw: int
    label: str


def decode_car_config(raw: bytes, cmap: dict) -> list:
    """Decodes the 0xFC car-configuration block: one option per byte at a fixed
    offset, mapped to its label. Options whose byte lies past the response are
    skipped."""
    options: list = []
    for entry in (cmap.get("car_config") or {}).get("options", []):
        index = int(entry["byte"])
        if index >= len(raw):
            continue
        value = raw[index]
        values = entry.get("values") or {}
        label = values.get(value) or values.get(int(value))
        options.append(Option(entry["name"], value, label or ""))
    return options


def decode_installed_modules(raw: bytes, cmap: dict) -> list:
    """Decodes the 0xC010 block: each catalogued byte offset names a control
    unit; a non-zero byte means it is fitted."""
    modules: list = []
    for entry in (cmap.get("installed_modules") or {}).get("modules", []):
        index = int(entry["offset_bits"]) // 8
        if index >= len(raw):
            continue
        modules.append(Option(entry["name"], raw[index], "fitted" if raw[index] else "absent"))
    return modules
