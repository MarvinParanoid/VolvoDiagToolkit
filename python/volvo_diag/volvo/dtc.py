"""The trouble-code catalogue: Volvo's 16-bit codes to their VIDA text.

The module reports a list of active 2-byte codes on the wire (see
`volvo_diag.protocol.volvo.read_dtcs`); this turns each code into a name using
the catalogue extracted from CarCom (`definitions/volvo/p1/dtc-*.yaml`).
"""

from __future__ import annotations

from pathlib import Path

import yaml


def _find(name: str) -> Path | None:
    here = Path(__file__).resolve()
    for base in here.parents:
        candidate = base / "definitions" / "volvo" / "p1" / name
        if candidate.exists():
            return candidate
    return None


def load_catalogue(ecu: str = "ECM") -> dict:
    """The {code_string: text} map for a module, or an empty map if none is
    bundled. Codes are 4-hex-digit strings, upper-case."""
    path = _find(f"dtc-{ecu.lower()}.yaml")
    if path is None:
        return {}
    with path.open("r", encoding="utf-8") as handle:
        doc = yaml.safe_load(handle) or {}
    return {str(k).upper(): v for k, v in (doc.get("codes") or {}).items()}


def describe(code: int, catalogue: dict) -> str:
    """The catalogue text for a 16-bit code, or '' if it is not listed."""
    return catalogue.get(f"{code:04X}", "")
