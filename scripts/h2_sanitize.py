#!/usr/bin/env python3
"""Sanitize H2 hardware-test artifacts before sharing them.

    python scripts/h2_sanitize.py --vin YV1... [--mask STR ...] --out DIR FILE...

Inputs may be proxy JSONL logs (.jsonl), `dump` JSON backups (.json) and
console transcripts (anything else). What is masked, and how:

* the VIN, its chassis part (last 6 characters), every `--mask` string, and any
  value printed next to an identifying label (VIN, FYON, Chassis, Package
  identity, Car configuration document number, ... serial) in the inputs;
* in proxy logs, PASSTHRU_MSG data is matched on each CAN id's *reassembled
  payload stream*, so an identifier split across 8-byte frames is still found.
  The 4-byte CAN id and each frame's first byte (length marker / frame
  control) are never touched; only data bytes become 'X' (0x58). ISO15765
  messages are matched whole;
* whole sensitive B9 blocks are masked, data bytes only (answers on the
  0x40xxxx/0x60xxxx diagnostic response ids): F8/E8 (ECU/BCM serial number)
  and 01/02 (immobiliser PIN / security code, see docs/volvo-protocol.md);
* UDS ReadDataByIdentifier answers for ECU serial (62 F18C) and VIN (62 F190)
  in ISO15765 messages, data bytes only;
* user names in paths (C:\\Users\\<name>, /home/<name>) and the device-name
  string an application passed to PassThruOpen.

Hex fields are only ever changed byte-for-byte, so they stay valid hex with
the same length and offsets.

This masks KNOWN identifiers only. It never declares a file "clean": it lists
the B9 blocks and UDS identifiers it passed through unmasked so you can judge
them, and anything not listed above is left alone unless you pass it with
--mask. Review the output before posting it.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SERIAL_BLOCKS = {0xF8, 0xE8,          # B9 block ids: ECU / BCM serial number
                 0x01, 0x02}          # immobiliser PIN / security code
UDS_SENSITIVE_DIDS = {0xF18C, 0xF190}  # ECU serial number, VIN
POSITIVE_BLOCK = 0xF9                 # B9 + 0x40
RAW_CAN = {5, 0x8004}                 # payload = [ctrl][Volvo bytes]
ISO15765 = 6
X = 0x58
LABELS = re.compile(
    r"(?im)^[ \t]*(?:VIN|FYON|Chassis|Package identity|Car configuration document number|"
    r"[\w ]*serial(?: number)?)[ \t]*[:=]?[ \t]+([A-Za-z0-9][A-Za-z0-9\-]{3,})[ \t]*$")
USER_PATH = re.compile(r"(?i)([A-Z]:(?:\\\\|\\|/)+(?:Users|Documents and Settings)(?:\\\\|\\|/)+"
                       r"|/home/)([^\\/\"'\s]+)")
HEX_RUN = re.compile(r"[0-9A-Fa-f]{8,}")


def identifiers(vin: str, extra: list, texts: list) -> list:
    found = {vin.upper(), vin.upper()[-6:]} if vin else set()
    found |= {m.strip() for m in extra if m.strip()}
    for text in texts:
        found |= {m.group(1) for m in LABELS.finditer(text)}
    return sorted((f for f in found if len(f) >= 4), key=len, reverse=True)


def mask_bytes(buf: bytearray, idents: list) -> None:
    """Overwrite each identifier's ASCII occurrences in buf with 'X'."""
    upper = bytes(buf).upper()
    for ident in idents:
        needle = ident.upper().encode("latin-1")
        at = upper.find(needle)
        while at >= 0:
            buf[at:at + len(needle)] = bytes([X]) * len(needle)
            at = upper.find(needle, at + 1)


def mask_str(text: str, idents: list) -> str:
    """Plain text: user paths, identifiers as text, and identifiers hex-encoded
    inside hex runs (byte-aligned to the run, so the run stays valid hex)."""
    text = USER_PATH.sub(lambda m: m.group(1) + "<user>", text)
    for ident in idents:
        text = re.sub(re.escape(ident), "X" * len(ident), text, flags=re.I)

    def runs(m):
        run = m.group(0)
        if len(run) % 2:
            return run
        buf = bytearray.fromhex(run)
        mask_bytes(buf, idents)
        out = buf.hex()
        return out.upper() if run.isupper() or run.isdigit() else out
    return HEX_RUN.sub(runs, text)


def walk(obj, idents: list, skip: set):
    """Mask every string in a JSON value except the keys in `skip`."""
    if isinstance(obj, dict):
        return {k: (v if k in skip else walk(v, idents, skip)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [walk(v, idents, skip) for v in obj]
    if isinstance(obj, str):
        return mask_str(obj, idents)
    return obj


def sanitize_jsonl(lines: list, idents: list, seen: dict | None = None) -> list:
    """Sanitized lines. `seen`, if given, collects what was passed through
    unmasked for review: {"b9_blocks": set(), "uds_dids": set()}."""
    seen = seen if seen is not None else {"b9_blocks": set(), "uds_dids": set()}
    records = [json.loads(line) for line in lines if line.strip()]
    streams: dict = {}                       # can id -> [(buffer, index), ...]
    in_serial: dict = {}                     # can id -> inside an F8/E8 answer
    buffers = []
    for r in records:
        for m in r.get("msgs") or []:
            data = bytearray.fromhex(m.get("data", ""))
            buffers.append((m, data))
            if len(data) < 5:
                continue
            if m.get("proto") == ISO15765:
                if len(data) >= 7 and data[4] == 0x62:       # ReadDataByIdentifier answer
                    did = int.from_bytes(data[5:7], "big")
                    if did in UDS_SENSITIVE_DIDS:
                        data[7:] = bytes([X]) * (len(data) - 7)
                    else:
                        seen["uds_dids"].add(f"{did:04X}")
                mask_bytes(data, idents)    # a whole UDS message
                continue
            if m.get("proto") not in RAW_CAN:
                continue
            can_id = int.from_bytes(data[:4], "big")
            payload = data[4:]
            first = 1
            if (can_id >> 16) in (0x40, 0x60) and payload[0] >> 4 in (0x8, 0x9, 0xC):
                # a first/single frame starts a new answer on this id
                is_block = len(payload) >= 4 and payload[2] == POSITIVE_BLOCK
                in_serial[can_id] = is_block and payload[3] in SERIAL_BLOCKS
                if is_block and not in_serial[can_id]:
                    seen["b9_blocks"].add(f"{payload[3]:02X}")
                if in_serial[can_id]:
                    first = 4                # keep [ctrl][comm][F9][block]
            if in_serial.get(can_id):
                data[4 + first:] = bytes([X]) * (len(data) - 4 - first)
            streams.setdefault(can_id, []).extend((data, k) for k in range(5, len(data)))
    for refs in streams.values():
        joined = bytearray(buf[k] for buf, k in refs)
        mask_bytes(joined, idents)
        for (buf, k), b in zip(refs, joined):
            buf[k] = b
    for m, data in buffers:
        m["data"] = data.hex().upper()
    out = []
    for r in records:
        if r.get("ev") == "open" and r.get("name"):
            r["name"] = "<device>"
        msgs = r.pop("msgs", None)
        r = walk(r, idents, {"mask", "pattern", "flow_control"})
        if msgs is not None:
            r["msgs"] = [walk(m, idents, {"data"}) for m in msgs]
        out.append(json.dumps(r, separators=(",", ":")))
    return out


def sanitize_dump(obj: dict, idents: list) -> dict:
    blocks = {}
    for block, hexdata in (obj.get("blocks") or {}).items():
        buf = bytearray.fromhex(hexdata)
        if int(block, 16) in SERIAL_BLOCKS:
            buf = bytearray([X]) * len(buf)
        else:
            mask_bytes(buf, idents)
        blocks[block] = buf.hex()
    out = walk(obj, idents, {"blocks"})
    if "blocks" in obj:
        out["blocks"] = blocks
    return out


def residual(text: str, idents: list) -> list:
    up = text.upper()
    return [i for i in idents
            if i.upper() in up or i.upper().encode("latin-1").hex().upper() in up]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vin", required=True)
    ap.add_argument("--mask", action="append", default=[],
                    help="another identifier to mask (repeatable)")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("files", nargs="+")
    args = ap.parse_args(argv)
    paths = [Path(f) for f in args.files]
    texts = [p.read_text(encoding="utf-8", errors="replace") for p in paths]
    idents = identifiers(args.vin, args.mask, texts)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    dirty = 0
    for path, text in zip(paths, texts):
        seen: dict = {"b9_blocks": set(), "uds_dids": set()}
        if path.suffix == ".jsonl":
            result = "\n".join(sanitize_jsonl(text.splitlines(), idents, seen)) + "\n"
        elif path.suffix == ".json":
            result = json.dumps(sanitize_dump(json.loads(text), idents), indent=2)
        else:
            result = mask_str(text, idents)
        (out / path.name).write_text(result, encoding="utf-8")
        left = residual(result, idents)
        dirty += bool(left)
        status = (f"RESIDUAL {left}" if left
                  else "no known identifier left (unknown ones may remain)")
        passed = [f"B9 blocks {sorted(seen['b9_blocks'])}" if seen["b9_blocks"] else "",
                  f"UDS DIDs {sorted(seen['uds_dids'])}" if seen["uds_dids"] else ""]
        passed = [x for x in passed if x]
        print(f"{path.name}: {status}"
              + (f"; passed through unmasked, review: {', '.join(passed)}" if passed else ""))
    print(f"masked {len(idents)} known identifier(s). This is NOT a guarantee of "
          f"anonymity: review {out} before sharing.")
    return 1 if dirty else 0


if __name__ == "__main__":
    sys.exit(main())
