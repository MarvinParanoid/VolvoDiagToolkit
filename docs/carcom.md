# Pulling parameters from VIDA's CarCom database

VIDA installs a local SQL Server (2008 R2 Express, instance `.\VIDA`) whose
`carcom` database holds every diagnostic parameter it knows: the identifier, a
scaling formula as text, a unit, a min/max range and a name — per ECU variant.
Reading it beats reverse engineering each parameter on the wire: it is exact,
it needs no car, and it covers parameters a live capture never happened to hit.

This is how the D4164T's ~40 engine parameters got into
`definitions/volvo/p1/d4164t.yaml`.

## Schema, in short

The join VIDA itself uses (from `vadis_GetParameterData`, dumped by the scripts):

* a **PARAM block** (`T141_Block`, `fkT142_BlockType = 8`) is a value: it has a
  byte offset/length, a name (`T190_Text`/`T191_TextData`), and via its
  `T150_BlockValue` a unit (`fkT190_Text_Unit`) and a scaling formula
  (`T155_Scaling.definition`, e.g. `(x-2731.4)/10`);
* it hangs off an **identifier block** (`T144_BlockChild` parent) whose
  `T150_BlockValue.CompareValue` is the on-wire identifier (`0x007E`), a `REID`
  block type;
* everything is scoped to an **EcuVariant** (`T100_EcuVariant`), and the CAN
  addressing (`canAddress` 0x0FFFFE, `commAddress` 0x11, `addressSize` 29) is in
  `T121_Config` — matching exactly what the proxy captured.

## The scripts

Run from a normal (non-elevated) PowerShell on the VM so Windows auth and the
`Z:`/`\\vmware-host` share both work. Each writes to `-OutDir` (put it on the
shared folder). All read-only.

1. `scripts/carcom-explore.ps1` — find the SQL instance, list databases, dump
   the parameter-table schema.
2. `scripts/carcom-procs.ps1` — dump the stored-procedure definitions (the join
   recipe) and the scaling/type/language lookups.
3. `scripts/carcom-ident.ps1` — dump `T121_Config` (the CAN addressing) and the
   request metadata.
4. `scripts/carcom-final.ps1` — the payoff: rank ECU variants by how many of
   our captured on-wire identifiers they carry, pick the match, and dump its
   full parameter set (identifier, offset, formula, unit, name, range) to
   `carcom-final.csv`.

The identifier that matches is what pins the exact variant: for the D4164T,
EcuVariant 486 ("ECM DV6b") carried all nine of our captured ids, while the
generic EDC16C variants only shared six by coincidence.

## From CSV to YAML

`carcom-final.csv` is converted to the parameter block by parsing each
`T155_Scaling` formula into a scale/offset (linear) or a bit index (`x&0b...`)
and mapping the well-known identifiers to the dashboard keys. The formulas
validated to the last decimal against the values captured on the car, so the
resulting definitions carry `verified-against-vida` (captured + formula agree)
or `verified` (from the database for the matched variant).

## What it does not give

Computed/aggregate values — soot load, regeneration state, distance since
regeneration — are not in the REID live-data list; VIDA derives them or reads
them through routine/DTC blocks (other `T142_BlockType`s). Those need either a
DPF-screen capture through the proxy or a deeper CarCom dig.

Security codes are also not really here: `vadis_GetSecurityCodeFromEcuType` is a
plain lookup in `T171_SecurityCode`, but for this car those rows are `FF`
placeholders — the real per-car CEM code is not in CarCom (it lives in the CEM
firmware). See [volvo-config-write status] in the notes.

## Alternative: vida_py

kForth's [`vida_py`](https://github.com/kForth/vida_py) (`pip install vida-py`) is
a typed SQLAlchemy interface to every VIDA database — a cleaner, cross-platform
alternative to the `carcom-*.ps1` scripts here. Point the `VIDA_*_DB_URI` env
vars at the attached databases and query the same tables (`T171_SecurityCode`,
`T121_Config`, the `vadis_*` procs) from Python. `scripts/vida_py_hunt.py` uses
it to dump a variant's security codes and scan the session DB for a real code.
