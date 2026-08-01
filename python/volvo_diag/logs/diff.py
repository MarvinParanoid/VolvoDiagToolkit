"""What changed between two proxy logs.

The point of stage 4: record a baseline, add exactly one parameter in VIDA,
record again, and let this tell you which request appeared.

    python -m volvo_diag.logs.diff 01-baseline.jsonl 02-plus-boost.jsonl

VIDA often groups several parameters into one request, so a new parameter does
not have to show up as a new key — it can also lengthen an existing request or
its answer. Both are reported.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .parser import load, pair
from .summarize import RequestStats, collect


def _by_key(path: Path) -> dict[str, RequestStats]:
    """Request stats keyed by request only — CAN id is reported, not keyed on,
    so the same request to the same ECU on a renumbered channel still matches."""
    log = load(path)
    merged: dict[str, RequestStats] = {}
    for (_, can_id, key), entry in collect(pair(log.frames)).items():
        existing = merged.get(key)
        if existing is None:
            merged[key] = entry
        else:
            existing.count += entry.count
            existing.answered += entry.answered
            existing.negative += entry.negative
            existing.response_lengths |= entry.response_lengths
            existing.request_lengths |= entry.request_lengths
            if not existing.example_response:
                existing.example_response = entry.example_response
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--min-count", type=int, default=1,
                        help="ignore requests seen fewer times than this")
    args = parser.parse_args(argv)

    before = _by_key(args.baseline)
    after = _by_key(args.candidate)

    added = {k: v for k, v in after.items() if k not in before and v.count >= args.min_count}
    removed = {k: v for k, v in before.items() if k not in after and v.count >= args.min_count}
    common = sorted(set(before) & set(after))

    print(f"baseline   {args.baseline}   {len(before)} distinct requests")
    print(f"candidate  {args.candidate}   {len(after)} distinct requests")

    if added:
        print(f"\nonly in {args.candidate.name}:")
        for key, entry in sorted(added.items(), key=lambda kv: -kv[1].count):
            cid = f"{entry.can_id:03X}" if entry.can_id is not None else "-"
            lengths = "/".join(str(n) for n in sorted(entry.response_lengths)) or "-"
            print(f"  CAN 0x{cid}  {_spaced(key):<14} {entry.count:>5}x  "
                  f"resp len {lengths:<6} {entry.example_response[:48]}")
    else:
        print(f"\nno new requests in {args.candidate.name}")

    if removed:
        print(f"\nonly in {args.baseline.name}:")
        for key, entry in sorted(removed.items(), key=lambda kv: -kv[1].count):
            print(f"  {_spaced(key):<14} {entry.count:>5}x")

    shape_changes = []
    for key in common:
        b, a = before[key], after[key]
        if b.response_lengths != a.response_lengths or b.request_lengths != a.request_lengths:
            shape_changes.append((key, b, a))
    if shape_changes:
        print("\nsame request, different shape (VIDA regrouped the parameters):")
        for key, b, a in shape_changes:
            print(f"  {_spaced(key):<14} "
                  f"req {sorted(b.request_lengths)}->{sorted(a.request_lengths)}  "
                  f"resp {sorted(b.response_lengths)}->{sorted(a.response_lengths)}")

    rate_changes = []
    for key in common:
        b, a = before[key], after[key]
        if b.count and abs(a.count - b.count) / b.count > 0.5 and abs(a.count - b.count) >= 10:
            rate_changes.append((key, b.count, a.count))
    if rate_changes:
        print("\npolling rate changed by more than half:")
        for key, was, now in sorted(rate_changes, key=lambda r: -(r[2] - r[1])):
            print(f"  {_spaced(key):<14} {was} -> {now}")

    return 0


def _spaced(key: str) -> str:
    return " ".join(key[i:i + 2] for i in range(0, len(key), 2))


if __name__ == "__main__":
    raise SystemExit(main())
