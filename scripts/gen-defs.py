#!/usr/bin/env python3
"""Generate supplementary parameter definitions from a CarCom export.

The curated per-module YAMLs hold a hand-picked, wire-verified subset. The
`carcom-*.ps1` extractors pull *every* readable identifier for the car's ECU
variant into a CSV. This turns the identifiers that are NOT already defined into
a `<module>-extra.yaml` of `candidate` parameters — real provenance (Volvo's own
scaling), but not yet confirmed on the wire, so they carry the weakest status
and never overwrite the curated files.

    python scripts/gen-defs.py            # regenerate every *-extra.yaml

CSV columns (from carcom-module.ps1): identifier, parenttype, byteoffset (bits),
bits/bytelength (bits), datatype, scaling (a formula in x), unit, name, min, max.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "logs"
DEFS = ROOT / "definitions" / "volvo" / "p1"

# module -> (csv, comm address / group, curated yaml to exclude, output, key prefix)
MODULES = [
    ("ECM", "carcom-ecm.csv", 0x11, "d4164t.yaml", "d4164t-extra.yaml", "ecm"),
    ("CEM", "carcom-cem.csv", 0x50, "cem.yaml", "cem-extra.yaml", "cem"),
    ("DIM", "carcom-dim.csv", 0x51, "dim.yaml", "dim-extra.yaml", "dim"),
    ("ABS", "carcom-abs.csv", 0x01, "abs.yaml", "abs-extra.yaml", "abs"),
]

# low-speed modules whose only export is the enumerated-state config catalogue
# (carcom-config.ps1 / carcom-phm.ps1 output): identifier + value -> label rows.
ENUM_MODULES = [
    ("ICM", "carcom-config-icm.csv", 0x54, "icm-extra.yaml", "icm"),
    ("BPM", "carcom-phm-bt.csv", 0x7C, "bpm-extra.yaml", "bpm"),
]

TYPE = {("Unsigned", 8): "uint8", ("Unsigned", 16): "uint16_be",
        ("Unsigned", 24): "uint24_be", ("Unsigned", 32): "uint32_be",
        ("Signed", 8): "int8", ("Signed", 16): "int16_be", ("Signed", 32): "int32_be"}


def parse_scaling(formula: str):
    """A CarCom scaling formula -> {mask, scale, offset}, or None if unparseable.

    Handles the linear part by evaluating f(0) and f(1); a leading `x & MASK`
    is split off first (it is applied before the scale)."""
    formula = (formula or "x").strip() or "x"
    mask = None
    var, expr = "x", formula
    if "&" in formula:
        m = re.match(r"x\s*&\s*(0x[0-9a-fA-F]+|0b[01]+|\d+)(.*)$", formula)
        if not m:
            return None
        mask = int(m.group(1), 0)
        var, expr = "y", "y" + m.group(2).strip()

    def ev(value: float):
        e = re.sub(r"\b" + var + r"\b", repr(value), expr)
        # only numbers/operators (0x/0b are fine for eval) — no names slip through
        if re.search(r"[a-wzA-WZ]", e.replace("0x", "").replace("0b", "")):
            raise ValueError(e)
        return eval(e, {"__builtins__": {}}, {})  # noqa: S307 — our own DB formulas

    try:
        f0, f1 = ev(0), ev(1)
    except Exception:
        return None
    return {"mask": mask, "scale": _clean(f1 - f0), "offset": _clean(f0)}


def _clean(x: float):
    x = round(float(x), 10)
    return int(x) if x == int(x) else x


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return s or "param"


def existing_keys_and_ids() -> tuple[set, dict]:
    """Keys already used anywhere, and per-module {group: {(ident, byte_off)}}."""
    keys, ids = set(), {}
    for f in DEFS.glob("*.yaml"):
        if f.name.endswith("-extra.yaml"):
            continue  # never exclude against our own generated output (idempotent)
        raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        for key, entry in (raw.get("parameters") or {}).items():
            keys.add(key)
            entry = entry or {}
            if str(entry.get("protocol")) == "volvo" and entry.get("identifier") is not None:
                g = int(entry["group"])
                bo = int((entry.get("encoding") or {}).get("byte_offset", 0))
                ids.setdefault(g, set()).add((int(entry["identifier"]), bo))
    return keys, ids


def q(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def gen_enum(module, csv_name, group, out_name, prefix, used_keys) -> None:
    """Enumerated states (config catalogue: identifier + value -> label) to enum
    parameters. Used for the low-speed diagnostic modules (ICM, BPM)."""
    path = LOGS / csv_name
    if not path.exists():
        print(f"skip {module}: {csv_name} not found")
        return
    rows = list(csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines(), delimiter="\t"))
    groups, order = {}, []
    for r in rows:
        param = (r.get("param") or "").strip()
        val, lab = (r.get("value") or "").strip(), (r.get("valuetext") or "").strip()
        if not param or not val or not lab or lab in ("Undefined value", "Parameter error"):
            continue
        try:
            iv = int(val, 0)
        except ValueError:
            continue
        key = (int(r["identifier"], 16), int(r.get("bitoffset") or 0), param)
        if key not in groups:
            groups[key] = {"values": {}, "bits": int(r.get("bitlen") or 8),
                           "mask": (parse_scaling(r.get("scaling")) or {}).get("mask")}
            order.append(key)
        groups[key]["values"].setdefault(iv, lab)

    lines, n = [], 0
    for ident, off_bits, param in order:
        g = groups[(ident, off_bits, param)]
        if not g["values"]:
            continue
        byte_off, length = off_bits // 8, max(1, g["bits"] // 8)
        key = f"{prefix}_{_slug(param)}"
        if byte_off:
            key = f"{key}_{byte_off}"
        base, i = key, 2
        while key in used_keys:
            key, i = f"{base}_{i}", i + 1
        used_keys.add(key)
        vals = ", ".join(f"{iv}: {q(g['values'][iv])}" for iv in sorted(g["values"]))
        lines += [f"  {key}:", f"    name: {q(param)}", f"    ecu: {module}",
                  "    protocol: volvo", f"    group: 0x{group:02X}",
                  f"    identifier: 0x{ident:04X}", "    encoding:", "      type: enum"]
        if length != 1:
            lines.append(f"      length: {length}")
        if byte_off:
            lines.append(f"      byte_offset: {byte_off}")
        if g["mask"] is not None:
            lines.append(f"      mask: 0x{g['mask']:X}")
        lines += ["      values: {" + vals + "}", "    status: candidate",
                  f"    source: {q(f'VIDA CarCom, {module} config catalogue; not yet confirmed on wire')}"]
        n += 1

    header = [f"# {module} — enumerated states from CarCom (config catalogue).",
              "# Generated by scripts/gen-defs.py; status 'candidate'.", "", "parameters:"]
    (DEFS / out_name).write_text("\n".join(header + lines) + "\n", encoding="utf-8")
    print(f"{module}: wrote {n} enum params -> {out_name}")


def main() -> None:
    used_keys, defined = existing_keys_and_ids()
    for module, csv_name, group, _curated, out_name, prefix in MODULES:
        path = LOGS / csv_name
        if not path.exists():
            print(f"skip {module}: {csv_name} not found")
            continue
        rows = list(csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines(),
                                   delimiter="\t"))
        already = defined.get(group, set())
        lines, n = [], 0
        for r in rows:
            ident = int(r["identifier"], 16)
            off_bits = int(r.get("byteoffset") or 0)
            byte_off = off_bits // 8
            if (ident, byte_off) in already:
                continue
            already.add((ident, byte_off))
            bits = int(r.get("bits") or r.get("bytelength") or 16)
            etype = TYPE.get((r.get("datatype"), bits))
            enc = parse_scaling(r.get("scaling"))
            if etype is None or enc is None:
                continue  # a datatype/formula we cannot express faithfully — skip
            name = (r.get("name") or "").strip() or f"{module} {ident:#06x}"
            key = f"{prefix}_{_slug(name)}"
            if byte_off:
                key = f"{key}_{byte_off}"
            base, i = key, 2
            while key in used_keys:
                key, i = f"{base}_{i}", i + 1
            used_keys.add(key)

            parts = [f"type: {etype}"]
            if enc["scale"] != 1:
                parts.append(f"scale: {enc['scale']}")
            if enc["offset"] != 0:
                parts.append(f"offset: {enc['offset']}")
            if enc["mask"] is not None:
                parts.append(f"mask: 0x{enc['mask']:X}")
            if byte_off:
                parts.append(f"byte_offset: {byte_off}")
            unit = (r.get("unit") or "").strip()
            lines.append(f"  {key}:")
            lines.append(f"    name: {q(name)}")
            lines.append(f"    ecu: {module}")
            lines.append("    protocol: volvo")
            lines.append(f"    group: 0x{group:02X}")
            lines.append(f"    identifier: 0x{ident:04X}")
            lines.append("    encoding: {" + ", ".join(parts) + "}")
            # A candidate may not claim a unit (project rule: an unverified unit
            # is a false claim about physics). Keep CarCom's unit in the source
            # note so it is not lost — promote it to a real `unit:` on verifying.
            lines.append("    status: candidate")
            src = f"VIDA CarCom, {module} variant; not yet confirmed on wire"
            if unit:
                src += f"; CarCom unit '{unit}'"
            lines.append(f"    source: {q(src)}")
            n += 1

        header = [
            f"# {module} — supplementary parameters from CarCom, NOT in the curated",
            f"# {_curated}. Generated by scripts/gen-defs.py; status 'candidate' — the",
            "# scaling is Volvo's own but these have not been confirmed on the wire.",
            "# Promote an entry into the curated file once verified.",
            "",
            "parameters:",
        ]
        (DEFS / out_name).write_text("\n".join(header + lines) + "\n", encoding="utf-8")
        print(f"{module}: wrote {n} extra params -> {out_name}")

    for module, csv_name, group, out_name, prefix in ENUM_MODULES:
        gen_enum(module, csv_name, group, out_name, prefix, used_keys)


if __name__ == "__main__":
    main()
