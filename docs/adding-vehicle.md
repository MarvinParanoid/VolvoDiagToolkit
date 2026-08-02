# Adding parameters and vehicles

The parameter database (`definitions/volvo/`) is filled two ways, both keeping
VIDA as the source of truth:

```
              ┌──► JSONL log ──► summarize / diff ─────────────┐
VIDA ─► j2534proxy.dll ─► VXDIAG ─► car                        │
              └──► connect/pins/protocol per bus ──────────┐   │
                                                           ▼   ▼
CarCom (VIDA's own SQL DB) ─► scripts/carcom-*.ps1 ─► definitions/*.yaml
                                                           │
                          volvo-monitor  (serve · monitor · config) ◄┘
```

- **From the proxy logs** — record a VIDA session, diff two recordings, decode
  what changed. Slow but needs nothing but the car.
- **From CarCom** — VIDA's parameter definitions live in a local SQL Server
  database. `scripts/carcom-*.ps1` pull the identifiers, scaling, value labels,
  DTC catalogue and per-bus connection details straight out of it, pinned to the
  exact car's ECU variants. This is how most of the database was built.

## From the proxy logs: record, diff, define

See [method.md](method.md) for the full method. Short version — one new
parameter per recording, then:

```sh
python -m volvo_diag.logs.diff 01-baseline.jsonl 02-plus-boost.jsonl
python -m volvo_diag.logs.summarize 02-plus-boost.jsonl --track 22D123
```

and write what you find into `definitions/volvo/p1/d4164t.yaml` with its status,
source log and a raw sample. (Set the proxy's `session_tag` per recording — see
[vida-proxy.md](vida-proxy.md).)

## From CarCom: extract in bulk

The `carcom-*.ps1` scripts read VIDA's local database directly. See
[carcom.md](carcom.md) for the schema and the workflow (explore → procs →
variants → per-module extraction). They produce the CSVs under `logs/`.

To turn those CSVs into definitions for the identifiers not already curated:

```sh
python3 scripts/gen-defs.py     # regenerates every definitions/volvo/p1/*-extra.yaml
```

`gen-defs.py` parses Volvo's scaling formulas (linear and masked), maps the
datatypes and offsets, skips identifiers already defined in the curated files,
and writes the rest as **`candidate`** parameters — real provenance (Volvo's own
scaling) but not yet confirmed on the wire, so they carry the weakest status and
no `unit`. Promote one into the curated file once you verify it live.

## The definitions format and status ladder

Nothing goes in until there is provenance. Every parameter carries `status`,
`source` and, for the curated ones, a raw `sample`. Statuses, weakest to
strongest:

| status | meaning |
| --- | --- |
| `candidate` | seen in a source, meaning inferred, never checked (no `unit` allowed) |
| `discovered` | the car answers it, encoding still a guess |
| `experimental` | encoding plausible and stable over a drive |
| `verified` | cross-checked against a second source or physics |
| `verified-against-vida` | VIDA and this toolkit show the same number |

A Volvo parameter is a `protocol: volvo` entry with a `group` (comm address),
`identifier` and an `encoding` (`type`, and any of `scale`/`offset`/`mask`/
`byte_offset`). See [d4164t.yaml](../definitions/volvo/p1/d4164t.yaml).

## A different vehicle

The engine-specific part is data only. A new car means: find its ECU variants in
CarCom (its profile), run the extractors against those variants, and drop the
YAML under `definitions/volvo/<platform>/`. The transports, protocol and
dashboard are not car-specific.

## What is defined so far (V50 / D4164T)

| module | bus | parameters | source |
| --- | --- | --- | --- |
| ECM (D4164T, Bosch EDC16C31) | 500k | boost, MAF, EGR, rail, DPF, temperatures, … | CarCom + verified against VIDA |
| ABS | 500k | wheel speeds, yaw, pressures | CarCom |
| CEM | 500k / 125k | electrical, climate, lighting, immobiliser states | CarCom |
| DIM | 125k | fuel, distance, cluster temperatures | CarCom |
| CEM configuration | — | VIN + 99 car-config options + installed-modules map | CarCom (`config-cem.yaml`) |
| ECM DTC catalogue | — | 154 fault-code → text entries | CarCom (`dtc-ecm.yaml`) |

## Transports and buses

Which transport does what, the capability matrix, and which modules answer on
which bus are in [transports.md](transports.md).
