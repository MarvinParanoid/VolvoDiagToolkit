#!/usr/bin/env python3
"""Hunt for a module's real security code using kForth's `vida_py` (a typed
Python interface to VIDA's databases) instead of raw PowerShell/SQL.

Run this ON THE VIDA MACHINE (it needs VIDA's MSSQL databases attached):

    pip install vida-py pyodbc
    set VIDA_CARCOM_DB_URI=mssql+pyodbc://sa:GunnarS3g3@localhost/carcom?driver=ODBC+Driver+17+for+SQL+Server
    set VIDA_SESSION_DB_URI=mssql+pyodbc://sa:GunnarS3g3@localhost/DiagSwdlSession?driver=ODBC+Driver+17+for+SQL+Server
    python scripts/vida_py_hunt.py --variant <CEM-variant-identifier>

What it does, and why: `vadis_GetSecurityCodeFromEcuType` is a plain lookup in
carcom.T171_SecurityCode, but for the V50 CEM those rows are FF placeholders — so
the real per-car code is not in carcom. This dumps (1) the carcom codes for the
variant (to confirm the placeholders), and (2) rows from the session DB's EcuInfo
/ SlaveEcuInfo, which is populated from the CAR during a VIDA session and is a
candidate place for the real code to appear. Read-only.
"""

from __future__ import annotations

import argparse
import json


def main() -> int:
    ap = argparse.ArgumentParser(description="vida_py security-code hunt (VIDA machine)")
    ap.add_argument("--variant", required=True,
                    help="ECU variant identifier (e.g. the CEM's), as in carcom")
    args = ap.parse_args()

    try:
        from vida_py.carcom import (
            Session,
            T100_EcuVariant,
            T170_SecurityCode_EcuVariant,
            T171_SecurityCode,
        )
    except ImportError:
        print("pip install vida-py  (and set the VIDA_*_DB_URI env vars)")
        return 2

    with Session() as session:
        variant = (session.query(T100_EcuVariant)
                   .filter(T100_EcuVariant.identifier == args.variant).one())
        codes = [
            {"code": s.code, "description": s.description,
             "type": s.type.identifier, "type_desc": s.type.description}
            for s in session.query(T171_SecurityCode)
            .outerjoin(T170_SecurityCode_EcuVariant,
                       T170_SecurityCode_EcuVariant.fkT171_SecurityCode == T171_SecurityCode.id)
            .filter(T170_SecurityCode_EcuVariant.fkT100_EcuVariant == variant.id).all()
        ]
    print("== carcom security codes for variant", args.variant)
    print(json.dumps(codes, indent=2, ensure_ascii=False))
    if codes and all(set(c["code"].replace("0", "")) <= {"F"} for c in codes if c["code"]):
        print("  -> all placeholders (FF): the real code is NOT in carcom.")

    # The session DB is filled from the car; a real code may surface here.
    try:
        from sqlalchemy import text
        from vida_py.session import Session as SessSession
        with SessSession() as s:
            for table in ("EcuInfo", "SlaveEcuInfo"):
                try:
                    rows = s.execute(text(f"SELECT TOP 20 * FROM {table}")).mappings().all()
                    print(f"\n== DiagSwdlSession.{table} ({len(rows)} rows)")
                    for r in rows:
                        print("  ", dict(r))
                except Exception as exc:  # noqa: BLE001
                    print(f"  ({table}: {exc})")
    except ImportError:
        print("\n(set VIDA_SESSION_DB_URI to also scan DiagSwdlSession.EcuInfo)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
