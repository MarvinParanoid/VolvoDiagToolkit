"""volvo-monitor — read the car without VIDA.

    volvo-monitor devices
    volvo-monitor info    --transport j2534
    volvo-monitor monitor --transport j2534 --csv trip.csv
    volvo-monitor dtc     --transport socketcan --channel can0
    volvo-monitor read 22F190 --transport j2534
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from pathlib import Path

from . import web
from .backend import VolvoBackend, open_volvo_ecm
from .categories import category_for
from .protocol import obd, uds
from .transport.base import Transport, TransportError
from .volvo import parameters as pdb
from .volvo.vehicle import Vehicle

log = logging.getLogger("volvo_diag")


# ---- transports ---------------------------------------------------------


def build_transport(args: argparse.Namespace) -> Transport:
    if args.transport == "j2534":
        from .transport.j2534 import J2534Transport

        return J2534Transport(args.library, baudrate=args.baudrate)
    if args.transport == "socketcan":
        from .transport.socketcan import SocketCanTransport

        return SocketCanTransport(args.channel)
    if args.transport == "elm":
        # ELM327 in standard-OBD/ISO-TP mode (UDS). The Volvo raw-A6 paths
        # intercept "elm" earlier and use ElmCanLink instead, so this branch is
        # only reached for UDS/OBD-II ECUs.
        from .transport.elm_obd import ElmObdTransport

        return ElmObdTransport(args.port)
    raise SystemExit(f"unknown transport {args.transport}")


def ecm_is_volvo(database: pdb.Database | None) -> bool:
    return database is not None and "ECM" in database.ecus and database.ecus["ECM"].is_volvo


class _Reader:
    """Reads individual parameters, hiding whether the ECM speaks UDS (Vehicle
    over a Transport) or the Volvo A6 protocol (VolvoEcm over a raw-CAN link).
    read_one(parameter) always returns a Reading."""

    def __init__(self, description: str, read_one, close, read_identity=None,
                 read_block=None, read_dtcs=None, clear_dtcs=None) -> None:
        self.description = description
        self.read_one = read_one  # (Parameter) -> Reading
        self.read_identity = read_identity  # (group) -> list[str], or None (UDS)
        self.read_block = read_block  # (identifier, group) -> bytes, or None (UDS)
        self.read_dtcs = read_dtcs  # (group) -> list[int], or None (UDS)
        self.clear_dtcs = clear_dtcs  # (group) -> bool, or None (UDS) — a WRITE
        self._close = close

    def __enter__(self) -> "_Reader":
        return self

    def __exit__(self, *exc: object) -> None:
        self._close()


def open_reader(args: argparse.Namespace, database: pdb.Database | None) -> _Reader:
    from .volvo.ecm import Reading

    if ecm_is_volvo(database) and args.transport in ("j2534", "elm"):
        from .transport.base import TransportError
        from .transport.volvo_ecm import VolvoEcm

        if args.transport == "elm":
            from .transport.elm_can import ElmCanLink
            link = ElmCanLink(args.port, baud=args.elm_baud)
        else:
            from .transport.j2534 import J2534CanLink
            link = J2534CanLink(args.library, baudrate=args.baudrate)
        link.open()
        ecm = VolvoEcm(link, group=database.ecus["ECM"].volvo_group)

        def read_one(parameter):
            try:
                return Reading(parameter, value=ecm.read(parameter))
            except TransportError as exc:
                return Reading(parameter, error=str(exc))

        return _Reader(f"{link.describe()} (Volvo A6)", read_one, link.close,
                       read_identity=ecm.read_identity, read_block=ecm.read_block,
                       read_dtcs=ecm.read_dtcs, clear_dtcs=ecm.clear_dtcs)

    transport = build_transport(args)
    transport.open()
    vehicle = Vehicle(transport, database)
    return _Reader(transport.describe(), lambda p: vehicle.ecm.read(p), transport.close)


def load_database(args: argparse.Namespace) -> pdb.Database | None:
    path = args.definitions or pdb.default_path()
    path = Path(path)
    if not path.exists():
        log.warning("no definitions at %s; standard OBD PIDs only", path)
        return None
    if path.is_file():
        return pdb.load(path)
    # A directory is a set of vehicle profiles: load only the selected one (plus
    # any shared files) so parameter keys never collide across cars.
    try:
        database = pdb.load_profile(path, getattr(args, "profile", None))
    except pdb.DefinitionError as exc:
        print(exc, file=sys.stderr)
        return None
    where = f" ({database.profile_id})" if database.profile_id else ""
    log.info("loaded %d parameters from %s%s", len(database), path, where)
    return database


# ---- commands -----------------------------------------------------------


def cmd_profiles(args: argparse.Namespace) -> int:
    """List the vehicle profiles available under the definitions directory."""
    root = Path(args.definitions or pdb.default_path())
    if not root.exists() or root.is_file():
        print(f"no profile directory at {root}", file=sys.stderr)
        return 1
    profiles = pdb.discover_profiles(root)
    if not profiles:
        print("no vehicle profiles (no vehicle.yaml found)")
        return 0
    default = next(iter(profiles)) if len(profiles) == 1 else None
    print(f"{len(profiles)} profile(s) in {root}:")
    for pid, directory in sorted(profiles.items()):
        mark = "  (default)" if pid == default else ""
        print(f"  {pid:28} {directory.relative_to(root)}{mark}")
    if not default:
        print("\nseveral profiles — select one with --profile <id>")
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    from .transport.j2534 import registered_devices

    devices = registered_devices()
    if not devices:
        print("no J2534 drivers registered (or not running on Windows)")
        return 1
    for device in devices:
        print(device)
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    database = load_database(args)
    with build_transport(args) as transport:
        vehicle = Vehicle(transport, database)
        print(f"transport   {transport.describe()}")

        j2534 = getattr(transport, "version", None)
        if callable(j2534):
            firmware, dll, api = transport.version()
            print(f"adapter     firmware {firmware}  dll {dll}  api {api}")
            millivolts = transport.battery_millivolts()
            if millivolts:
                print(f"battery     {millivolts / 1000:.2f} V")

        try:
            print(f"VIN         {vehicle.vin()}")
        except (TransportError, ValueError, uds.NegativeResponse) as exc:
            print(f"VIN         unavailable: {exc}")

        print("\nECM identification")
        for name, value in vehicle.ecm.identify().items():
            print(f"  {name:<40} {value}")

        print("\nsupported OBD-II PIDs")
        try:
            pids = vehicle.supported_pids()
        except TransportError as exc:
            print(f"  unavailable: {exc}")
            pids = []
        for pid in pids:
            entry = obd.PIDS.get(pid)
            label = f"{entry.name} [{entry.unit}]" if entry else "(no decoder here)"
            print(f"  {pid:02X}  {label}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    database = load_database(args)
    with build_transport(args) as transport:
        vehicle = Vehicle(transport, database)
        print("probing standard OBD addresses with TesterPresent...")
        for address, label in vehicle.scan():
            print(f"  {address}  {label}")
    return 0


def _cmd_dtc_volvo(args: argparse.Namespace, database: pdb.Database) -> int:
    """Reads active trouble codes from each Volvo-protocol module over the 0xAE
    service and names them from the bundled catalogue."""
    import datetime

    from .transport.base import TransportError
    from .volvo import dtc as dtcmod

    clear = getattr(args, "clear", False)
    if clear and not getattr(args, "enable_writes", False):
        print("dtc --clear is a WRITE (sends AF 11 to each module); "
              "re-run with --enable-writes to allow it.", file=sys.stderr)
        return 2
    catalogues: dict = {}
    total = 0
    with open_reader(args, database) as reader:
        modules = [(n, e.volvo_group) for n, e in database.ecus.items() if e.is_volvo]
        for name, group in sorted(modules, key=lambda m: m[1]):
            try:
                codes = reader.read_dtcs(group)
            except TransportError as exc:
                print(f"{name} (0x{group:02X}): {exc}")
                continue
            if not codes:
                print(f"{name} (0x{group:02X}): no codes")
            else:
                cat = catalogues.setdefault(name, dtcmod.load_catalogue(name, database.profile_dir))
                print(f"{name} (0x{group:02X}): {len(codes)} code(s)")
                for code in codes:
                    text = dtcmod.describe(code, cat) or "(not in catalogue)"
                    print(f"  {code:04X}  {text}")
                total += len(codes)
            if clear and reader.clear_dtcs is not None:
                stamp = datetime.datetime.now().strftime("%H:%M:%S")
                ok = reader.clear_dtcs(group)          # WRITE: AF 11
                after = reader.read_dtcs(group)         # confirm by re-reading
                state = "cleared" if (ok and not after) else ("ack, still %d" % len(after)
                                                               if ok else "no ack")
                print(f"  → WRITE clear {name} (0x{group:02X}) @ {stamp}: {state}")
    print(f"\n{total} active code(s) total" + (" · clear requested" if clear else ""))
    return 0


def cmd_dtc(args: argparse.Namespace) -> int:
    database = load_database(args)
    if ecm_is_volvo(database) and args.transport in ("j2534", "elm"):
        return _cmd_dtc_volvo(args, database)

    with build_transport(args) as transport:
        vehicle = Vehicle(transport, database)
        ecu = vehicle.ecu(args.ecu)
        try:
            dtcs = ecu.dtcs()
        except (TransportError, uds.NegativeResponse, uds.UnexpectedResponse) as exc:
            print(f"cannot read DTCs from {args.ecu}: {exc}")
            return 1
        if not dtcs:
            print(f"{args.ecu}: no stored codes")
            return 0
        print(f"{args.ecu}: {len(dtcs)} code(s)")
        for dtc in dtcs:
            flags = []
            if dtc.confirmed:
                flags.append("confirmed")
            if dtc.pending:
                flags.append("pending")
            print(f"  {dtc.text}  status 0x{dtc.status:02X}  {' '.join(flags)}")
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    database = load_database(args)
    target = args.what

    # A known parameter goes through the reader, so it works for the Volvo
    # protocol as well as UDS.
    if database and target in database.parameters:
        parameter = database[target]
        with open_reader(args, database) as reader:
            reading = reader.read_one(parameter)
        print(f"{parameter.name} [{parameter.status}]")
        print(f"  value     {reading.error or parameter.format(reading.value)}")
        return 0 if reading.ok else 1

    # A raw hex payload is UDS/OBD only (meaningful on an ECM that answers it).
    with build_transport(args) as transport:
        vehicle = Vehicle(transport, database)
        ecu = vehicle.ecu(args.ecu)
        payload = bytes.fromhex(target.replace(" ", ""))
        try:
            response = ecu.request(payload)
        except TransportError as exc:
            print(f"{target}: {exc}")
            return 1
        print(f"  request   {payload.hex().upper()}")
        print(f"  response  {response.hex().upper()}")
        if len(response) >= 3 and response[0] == 0x7F:
            print(f"  negative  {uds.NRC.get(response[2], hex(response[2]))}")
        return 0


# A short, useful default when the user does not pick parameters: the DPF and
# core engine readouts, in a sensible order. Only those present in the loaded
# database are shown.
DEFAULT_MONITOR_KEYS = (
    "rpm", "coolant_temperature", "boost_actual", "boost_requested",
    "dpf_differential_pressure", "exhaust_temperature", "dpf_temperature_upstream",
    "regeneration_active", "fuel_rail_pressure", "maf", "air_mass_per_stroke",
    "egr_valve_position", "egr_duty_cycle", "turbo_control_duty", "throttle_position",
    "intake_air_temperature", "barometric_pressure", "battery_voltage", "vehicle_speed",
)


def select_monitor_params(database: pdb.Database, args) -> list:
    """Resolves which parameters to poll from --params / --all / the default."""
    if args.params:
        wanted = [k.strip() for k in args.params.split(",") if k.strip()]
        params = []
        for key in wanted:
            if key in database.parameters:
                params.append(database[key])
            else:
                print(f"unknown parameter {key!r} (try `params` to list them)", file=sys.stderr)
        return params
    if args.all:
        return sorted((p for p in database if p.ecu.upper() == args.ecu.upper()),
                      key=lambda p: p.identifier if p.is_volvo else 0)
    # default highlights, in the given order, whichever exist
    return [database[k] for k in DEFAULT_MONITOR_KEYS if k in database.parameters]


def group_readings(readings: list) -> list:
    """Groups readings into ordered (label, [reading]) sections, preserving the
    given order within each section."""
    order = {}
    for reading in readings:
        rank, label = category_for(reading.parameter)
        order.setdefault((rank, label), []).append(reading)
    return [(label, items) for (rank, label), items in sorted(order.items())]


def render_table(readings: list, width: int = 32, grouped: bool = True) -> str:
    def row(reading) -> str:
        name = reading.parameter.name[:width]
        if reading.ok:
            value = reading.parameter.format(reading.value)
        elif "timeout" in reading.error.lower() or "no answer" in reading.error.lower():
            value = "-- (no answer)"
        else:
            value = f"-- ({reading.error[:24]})"
        return f"    {name:<{width}} {value:>16}   {reading.parameter.status}"

    if not grouped:
        return "\n".join(row(r) for r in readings)

    lines = []
    for label, items in group_readings(readings):
        lines.append(f"  {label}")
        lines.extend(row(r) for r in items)
    return "\n".join(lines)


def _make_refresh():
    """Returns a callable that repositions the cursor for a live redraw,
    portably. Uses ANSI where the terminal supports it (Linux, and Windows 10+
    once VT processing is enabled); falls back to clearing the screen on the
    old Windows 7 console, which does not understand ANSI escapes."""
    import os

    ansi = os.name != "nt"
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
                if kernel32.SetConsoleMode(handle, mode.value | 0x0004):
                    ansi = True
        except Exception:  # noqa: BLE001 — any failure means no ANSI
            ansi = False

    def refresh(first: bool, lines: int) -> None:
        if first:
            return
        if ansi:
            print(f"\033[{lines}A", end="")
        else:
            os.system("cls")  # Windows 7 console: full clear each frame

    return refresh


def cmd_monitor(args: argparse.Namespace) -> int:
    database = load_database(args)
    if database is None:
        print("no parameter database loaded", file=sys.stderr)
        return 1

    params = select_monitor_params(database, args)
    if not params:
        print("no parameters selected", file=sys.stderr)
        return 1

    writer = None
    handle = None
    if args.csv:
        handle = Path(args.csv).open("w", newline="", encoding="utf-8")
        writer = csv.writer(handle)
        writer.writerow(["t", *(p.key for p in params)])

    grouped = not args.no_group
    refresh = _make_refresh()
    started = time.monotonic()
    try:
        with open_reader(args, database) as reader:
            header = f"{reader.description}   {len(params)} parameters   ctrl-c to stop"
            first = True
            while True:
                readings = [reader.read_one(p) for p in params]
                table = render_table(readings, grouped=grouped)
                frame = (header + " " * 10 + "\n"
                         + f"  t = {time.monotonic() - started:8.1f} s" + " " * 40 + "\n"
                         + table)
                refresh(first, frame.count("\n") + 1)
                first = False
                print(frame)

                if writer:
                    values = {r.parameter.key: r.value for r in readings if r.ok}
                    writer.writerow([f"{time.monotonic() - started:.2f}",
                                     *[values.get(p.key, "") for p in params]])
                    handle.flush()
                if args.once:
                    break
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        if handle:
            handle.close()
            print(f"wrote {args.csv}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Serves the live dashboard as a local web page. Parameter selection, bus
    switching and the configuration view all live in the page itself."""
    if args.fake:
        backend = web.FakeBackend()
    else:
        database = load_database(args)
        if database is None:
            print("no parameter database loaded", file=sys.stderr)
            return 1
        if not (ecm_is_volvo(database) and args.transport in ("j2534", "elm")):
            print("the live dashboard needs the Volvo protocol (VXDIAG/J2534 or an "
                  "ELM327 via --transport elm); use --fake to preview it without a car",
                  file=sys.stderr)
            return 2
        backend = VolvoBackend(args, database)

    web.serve(backend, interval=args.interval, host=args.host, port=args.port)
    return 0


def cmd_identify(args: argparse.Namespace) -> int:
    """Reads the identity / configuration block (VIN, part numbers, software
    levels) from each Volvo-protocol module."""
    from .transport.base import TransportError

    database = load_database(args)
    if database is None:
        print("no parameter database loaded", file=sys.stderr)
        return 1

    with open_reader(args, database) as reader:
        if reader.read_identity is None:
            # UDS path: the standard identifiers.
            vehicle = Vehicle(build_transport(args), database)
            try:
                print(f"VIN   {vehicle.vin()}")
                for name, value in vehicle.ecm.identify().items():
                    print(f"  {name:<40} {value}")
            except (TransportError, ValueError) as exc:
                print(f"identify failed: {exc}")
                return 1
            return 0

        modules = [(n, e.volvo_group) for n, e in database.ecus.items() if e.is_volvo]
        for name, group in sorted(modules, key=lambda m: m[1]):
            try:
                fields = reader.read_identity(group)
            except TransportError as exc:
                print(f"{name} (0x{group:02X}): {exc}")
                continue
            print(f"\n{name} (comm 0x{group:02X}) — {len(fields)} fields")
            for index, field in enumerate(fields):
                tag = "  VIN" if index == 0 and field.startswith("YV") else f"  [{index}]"
                print(f"{tag:>6} {field}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """Checks whether an ELM327 adapter can actually do raw Volvo A6: 29-bit CAN,
    raw mode, a custom request id, and an ECM read — with latency and stability."""
    if args.transport != "elm":
        print("probe currently supports --transport elm only", file=sys.stderr)
        return 2
    from . import elm_probe

    try:
        report = elm_probe.probe(args.port, args.baud)
    except Exception as exc:  # noqa: BLE001 — surface serial/adapter errors plainly
        print(f"probe failed: {exc}", file=sys.stderr)
        return 1
    print(elm_probe.format_report(report))
    return 0 if report["verdict"] == "SUITABLE" else 1


def cmd_record(args: argparse.Namespace) -> int:
    """Records parameters to a CSV over time — one row per sample, a `t` column
    (seconds) then one column per parameter. Feed it to `analyze`."""
    database = load_database(args)
    if database is None:
        print("no parameter database loaded", file=sys.stderr)
        return 1
    params = select_monitor_params(database, args)  # --params / --all / diesel default
    if not params:
        print("no parameters selected", file=sys.stderr)
        return 1

    with open_reader(args, database) as reader, \
            Path(args.file).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["t", *(p.key for p in params)])
        started = time.monotonic()
        print(f"recording {len(params)} params to {args.file}  (ctrl-c to stop)")
        try:
            while True:
                readings = [reader.read_one(p) for p in params]
                row = [f"{time.monotonic() - started:.2f}"]
                for r in readings:
                    if r.ok and isinstance(r.value, bool):
                        row.append(1 if r.value else 0)
                    elif r.ok and isinstance(r.value, (int, float)):
                        row.append(r.value)
                    else:
                        row.append("")
                writer.writerow(row)
                handle.flush()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print(f"\nstopped — wrote {args.file}")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    """Post-drive report from a recorded trip CSV (see `record`)."""
    from . import analyze as trip

    times, cols = trip.load_trip(args.file)
    if not times:
        print("empty trip", file=sys.stderr)
        return 1
    roles = trip.detect_roles(cols)
    report = trip.analyze(times, cols, roles)
    print(trip.format_text(report, roles))
    if args.html:
        Path(args.html).write_text(trip.format_html(times, cols, roles, report),
                                   encoding="utf-8")
        print(f"wrote {args.html}")
    return 0


def cmd_dump(args: argparse.Namespace) -> int:
    """Reads a module's identity/configuration blocks and saves them verbatim to
    a JSON backup — a reference to compare against later (`diff`) or to support
    manual recovery with a proper programming tool. Read-only: it records what the
    module held, it cannot itself restore a module from the JSON."""
    import datetime
    import json as _json

    from .transport.base import TransportError
    from .volvo import config as configmod

    database = load_database(args)
    if database is None:
        print("no parameter database loaded", file=sys.stderr)
        return 1

    ecu = args.ecu.upper()
    if ecu in database.ecus:
        group = database.ecus[ecu].volvo_group
    elif args.group:
        group = int(args.group, 16)
    else:
        print(f"unknown ECU {ecu!r}; pass --group <hex commAddr>", file=sys.stderr)
        return 1

    if not (ecm_is_volvo(database) and args.transport == "j2534"):
        print("dump needs the Volvo protocol (VXDIAG / J2534)", file=sys.stderr)
        return 2
    block_ids = ([int(b, 16) for b in args.blocks.split(",") if b.strip()]
                 if args.blocks else [0xFB, 0xFC, 0xF5])
    # a module on a dedicated low-speed bus only answers there; the profile knows
    bus_id = args.bus or database.bus_for_module(ecu)
    try:
        database.bus(bus_id)
    except KeyError:
        ids = ", ".join(b.id for b in database.serve_buses())
        print(f"unknown bus {bus_id!r}; profile has: {ids}", file=sys.stderr)
        return 1

    link, ecm = open_volvo_ecm(args, database, bus_id)
    try:
        blocks, vin = {}, ""
        for bid in block_ids:
            try:
                raw = ecm.read_block(bid, group=group)
            except TransportError as exc:
                print(f"  0x{bid:02X}: {exc}")
                continue
            blocks[f"{bid:02X}"] = raw.hex()
            print(f"  0x{bid:02X}: {len(raw)} bytes")
            if bid == 0xFB and not vin:
                cmap = configmod.load_map(profile_dir=database.profile_dir)
                for field in configmod.decode_identity(raw, cmap):
                    if field.name == "VIN":
                        vin = field.value
            if bid == 0xF5:                          # part-number block
                for num, rev in configmod.decode_part_numbers(raw):
                    print(f"        part {num} rev {rev}")
        description = link.describe()
    finally:
        link.close()

    if not blocks:
        print("nothing read; is the car on and the right bus selected?", file=sys.stderr)
        return 1

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = {"vehicle": database.vehicle, "ecu": ecu, "group": f"0x{group:02X}",
              "bus": bus_id, "vin": vin, "captured": stamp, "reader": description,
              "blocks": blocks}
    out = args.out or f"dump-{ecu.lower()}-{stamp}.json"
    Path(out).write_text(_json.dumps(backup, indent=2), encoding="utf-8")
    print(f"wrote {out}  ({len(blocks)} block(s)"
          + (f", VIN {vin}" if vin else "") + ")")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    """Compares two module dumps (from `dump`) and shows the changed bytes and,
    for the identity/config blocks, which decoded field or option changed.
    Purely offline — the tool to check a change before/after."""
    import json as _json

    from .volvo import config as configmod

    a = _json.loads(Path(args.before).read_text(encoding="utf-8"))
    b = _json.loads(Path(args.after).read_text(encoding="utf-8"))
    ba, bb = a.get("blocks", {}), b.get("blocks", {})
    cmap = configmod.load_map()
    changed = False
    for name in sorted(set(ba) | set(bb)):
        xa = bytes.fromhex(ba.get(name, ""))
        xb = bytes.fromhex(bb.get(name, ""))
        if xa == xb:
            continue
        changed = True
        print(f"block 0x{name}: changed ({len(xa)} -> {len(xb)} bytes)")
        for i in range(max(len(xa), len(xb))):
            va = xa[i] if i < len(xa) else None
            vb = xb[i] if i < len(xb) else None
            if va != vb:
                sa = "--" if va is None else f"{va:02X}"
                sb = "--" if vb is None else f"{vb:02X}"
                print(f"  byte {i:>3}: {sa} -> {sb}")
        if name.upper() == "FC":
            oa = {o.name: o.label or f"0x{o.raw:02X}" for o in configmod.decode_car_config(xa, cmap)}
            ob = {o.name: o.label or f"0x{o.raw:02X}" for o in configmod.decode_car_config(xb, cmap)}
            for k in oa:
                if oa[k] != ob.get(k):
                    print(f"    {k}: {oa[k]} -> {ob.get(k)}")
        elif name.upper() == "FB":
            fa = {f.name: f.value for f in configmod.decode_identity(xa, cmap)}
            fb = {f.name: f.value for f in configmod.decode_identity(xb, cmap)}
            for k in fa:
                if fa[k] != fb.get(k):
                    print(f"    {k}: {fa[k]} -> {fb.get(k)}")
    if not changed:
        print("no differences")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    """Reads a module's programmed configuration (read-only): the vehicle
    identity (0xFB), the car configuration options (0xFC) and, best-effort, the
    installed-modules map (0xC010). Decode maps come from CarCom; only the
    identity block is verified against a capture."""
    from .transport.base import TransportError
    from .volvo import config as configmod

    database = load_database(args)
    cmap = configmod.load_map(args.config_map,
                              profile_dir=database.profile_dir if database else None)
    topo = database.config_topology() if database else pdb.DEFAULT_CONFIG
    group = (database.ecus[topo.ecu].volvo_group
             if database and topo.ecu in database.ecus else 0x50)
    fb, fc = topo.identity_block, topo.config_block

    with open_reader(args, database) as reader:
        if reader.read_block is None:
            print("configuration read needs the Volvo protocol (VXDIAG / J2534)")
            return 2

        def read_block_verified(ident, attempts=4):
            # These are large multi-frame blocks; a dropped frame shifts every
            # byte after it and the fixed-offset decode then prints garbage (a
            # diesel once showed "Fuel: Petrol"). verify=True rejects such a
            # block so we re-read; the final attempt drops verify so a systematic
            # trip degrades to a best-effort block rather than no output.
            last = None
            for i in range(attempts):
                try:
                    return reader.read_block(ident, group=group, timeout=2.0,
                                             verify=(i < attempts - 1))
                except TransportError as exc:
                    last = exc
            raise last

        try:
            raw_fb = read_block_verified(fb)
        except TransportError as exc:
            print(f"identity (0x{fb:02X}): {exc}")
            return 1
        print(f"Vehicle identity (0x{fb:02X})")
        for field in configmod.decode_identity(raw_fb, cmap):
            print(f"  {field.name:<32} {field.value}")

        try:
            raw_fc = read_block_verified(fc)
        except TransportError as exc:
            print(f"\ncar configuration (0x{fc:02X}): {exc}")
            return 0
        # Head (bytes 2..~40) verified against VIDA; the tail still drifts.
        print(f"\nCar configuration (0x{fc:02X}) — {len(raw_fc)} bytes  "
              f"[head verified vs VIDA; tail approximate]")
        for opt in configmod.decode_car_config(raw_fc, cmap):
            shown = opt.label or f"0x{opt.raw:02X}"
            print(f"  {opt.name:<32} {shown}")
    return 0


def cmd_params(args: argparse.Namespace) -> int:
    database = load_database(args)
    if database is None:
        return 1
    print(f"{len(database)} parameters from {len(database.sources)} file(s)")
    for parameter in sorted(database, key=lambda p: (p.ecu, p.key)):
        if parameter.is_volvo:
            addr = f"A6 {parameter.group:02X}/{parameter.identifier:04X}"
        else:
            addr = parameter.request.hex().upper()
        print(f"  {parameter.ecu:<5} {parameter.key:<28} {addr:<14} "
              f"{parameter.unit:<6} {parameter.status:<22} {parameter.source[:50]}")
    return 0


_WRITE_SERVICES = {0xB8, 0xBA, 0xAF, 0xA3}   # writes / clear / security — blocked here


def cmd_readmem(args: argparse.Namespace) -> int:
    """EXPERIMENTAL, read-only probe: send a read-by-address (0xBB) or raw A6
    request to a module and print the raw response frames. For hunting where a
    module keeps things (e.g. the CEM security PIN in flash). Framing is a
    hypothesis — tune --addr-bytes / --len against the car."""
    import time

    from .protocol import volvo

    database = load_database(args)
    if not (ecm_is_volvo(database) and args.transport == "j2534"):
        print("read-mem needs the Volvo protocol (VXDIAG / J2534)", file=sys.stderr)
        return 2
    ecu = args.ecu.upper()
    if ecu in database.ecus:
        comm = database.ecus[ecu].volvo_group
    elif args.group:
        comm = int(args.group, 16)
    else:
        print(f"unknown ECU {ecu!r}; pass --group <hex commAddr>", file=sys.stderr)
        return 1

    if args.raw:
        payload = bytes.fromhex(args.raw.replace(" ", ""))
    else:
        if args.addr is None:
            print("give --addr <hex> (and --len), or --raw <hex payload>", file=sys.stderr)
            return 1
        payload = (bytes([comm, int(args.service, 16)])
                   + int(args.addr, 16).to_bytes(args.addr_bytes, "big")
                   + bytes([args.len & 0xFF]))
    # Read-only guard: never let this send a write/clear/security service.
    if len(payload) >= 2 and payload[1] in _WRITE_SERVICES:
        print(f"refusing service 0x{payload[1]:02X}: read-mem is read-only", file=sys.stderr)
        return 2

    bus_id = args.bus or database.bus_for_module(ecu)
    link, _ecm = open_volvo_ecm(args, database, bus_id)
    frame = volvo.frame(payload)
    try:
        link.send(volvo.REQUEST_CAN_ID, frame)
        print(f"→ {frame.hex().upper()}   (comm 0x{comm:02X}, bus {bus_id})")
        deadline = time.monotonic() + args.timeout
        got = []
        while time.monotonic() < deadline:
            for can_id, data in link.receive(0.2):
                if volvo.is_response_canid(can_id):
                    got.append((can_id, data))
        if not got:
            print("  (no response — try a different --service/--addr-bytes, or wrong region)")
        for can_id, data in got:
            ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
            print(f"← {can_id:08X}  {data.hex().upper()}  |{ascii_}|")
    finally:
        link.close()
    return 0


def sniff_summary(frames: list) -> dict:
    """{can_id: (count, distinct_payloads)} for a list of (can_id, payload)."""
    seen: dict = {}
    for cid, pl in frames:
        count, payloads = seen.get(cid, (0, set()))
        payloads.add(bytes(pl))
        seen[cid] = (count + 1, payloads)
    return {cid: (c, len(p)) for cid, (c, p) in seen.items()}


def cmd_sniff(args: argparse.Namespace) -> int:
    """Passively dump CAN frames on a bus (read-only — sends nothing). Use it to
    find event frames: run it, trigger the event (open a door, take a call), and
    look for the id whose payload flips between a few values. The summary flags
    ids with 2–8 distinct payloads — the state/message frames."""
    import time

    database = load_database(args)
    if not (ecm_is_volvo(database) and args.transport == "j2534"):
        print("sniff needs the Volvo protocol (VXDIAG / J2534)", file=sys.stderr)
        return 2
    bus_id = args.bus or database.primary_bus().id
    want = int(args.id, 16) if args.id else None
    link, _ecm = open_volvo_ecm(args, database, bus_id)   # opens the bus; we only read
    seen: dict = {}
    frames: list = []
    print(f"sniffing bus {bus_id} for {args.duration:.0f}s "
          f"(Ctrl-C to stop) — trigger the event now…")
    deadline = time.monotonic() + args.duration
    try:
        while time.monotonic() < deadline:
            for can_id, data in link.receive(0.2):
                if want is not None and can_id != want:
                    continue
                data = bytes(data)
                frames.append((can_id, data))
                if args.all or seen.get(can_id) != data:   # print changes (or all)
                    asc = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
                    print(f"  {can_id:08X}  {data.hex().upper():<16}  |{asc}|")
                seen[can_id] = data
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        link.close()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            for cid, data in frames:
                fh.write(f"{cid:08X},{data.hex().upper()}\n")
        print(f"wrote {len(frames)} frames to {args.out}")
    stats = sniff_summary(frames)
    print(f"\n{len(stats)} ids, {len(frames)} frames:")
    for cid, (count, distinct) in sorted(stats.items()):
        flag = "   <- state/message frame?" if 2 <= distinct <= 8 else ""
        print(f"  {cid:08X}  {count:6}x  {distinct:3} distinct{flag}")
    return 0


def sniff_diff(before: list, after: list) -> list:
    """Byte positions that are constant within each capture but differ between
    them — the state bytes that a triggered event flipped. Rolling counters vary
    within a capture, so they are excluded. before/after are [(can_id, payload)].
    Returns [(can_id, byte_index, before_value, after_value)]."""
    def profile(frames):
        prof: dict = {}
        for cid, pl in frames:
            slots = prof.get(cid)
            if slots is None:
                slots = [set() for _ in range(len(pl))]
                prof[cid] = slots
            for i in range(min(len(pl), len(slots))):
                slots[i].add(pl[i])
        return prof

    pb, pa = profile(before), profile(after)
    out = []
    for cid in sorted(set(pb) & set(pa)):
        sb, sa = pb[cid], pa[cid]
        for i in range(min(len(sb), len(sa))):
            if len(sb[i]) == 1 and len(sa[i]) == 1 and sb[i] != sa[i]:
                out.append((cid, i, next(iter(sb[i])), next(iter(sa[i]))))
    return out


def _load_sniff(path: str) -> list:
    frames = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            cid, _, hexs = line.strip().partition(",")
            if hexs:
                frames.append((int(cid, 16), bytes.fromhex(hexs)))
    return frames


def cmd_sniffdiff(args: argparse.Namespace) -> int:
    """Compare two `sniff --out` captures and report the bytes that changed state
    between them (a door opened, a light came on) — counters filtered out."""
    diffs = sniff_diff(_load_sniff(args.before), _load_sniff(args.after))
    if not diffs:
        print("no stable byte changed between the two captures")
        return 0
    print(f"{len(diffs)} stable byte change(s):")
    for cid, i, b, a in diffs:
        print(f"  {cid:08X}  byte[{i}]  0x{b:02X} -> 0x{a:02X}")
    return 0


def cmd_dimtext(args: argparse.Namespace) -> int:
    """EXPERIMENTAL WRITE: broadcast a text string to the instrument-cluster (DIM)
    display by spoofing the phone/message module on the 125k cabin bus. The P1 ids
    are unknown, so you must supply --phm-id and --lcd-id (try candidates on-car).
    See docs/dim-display.md."""
    from .volvo import dim

    database = load_database(args)
    if not (ecm_is_volvo(database) and args.transport == "j2534"):
        print("dim-text needs the Volvo protocol (VXDIAG / J2534)", file=sys.stderr)
        return 2
    if not getattr(args, "enable_writes", False):
        print("dim-text is a WRITE (broadcast injection on the cabin bus); "
              "re-run with --enable-writes to allow it.", file=sys.stderr)
        return 2
    if args.year:                       # a model-year preset, unless ids are explicit
        preset_phm, preset_lcd = dim.PRESETS[args.year]
    if args.phm_id and args.lcd_id:
        phm_id, lcd_id = int(args.phm_id, 16), int(args.lcd_id, 16)
    elif args.year:
        phm_id, lcd_id = preset_phm, preset_lcd
    else:
        print(f"give --year {{{','.join(dim.PRESETS)}}} or both --phm-id and --lcd-id",
              file=sys.stderr)
        return 1
    bus_id = args.bus or database.bus_for_module("DIM")   # the cabin (125k) bus
    link, _ecm = open_volvo_ecm(args, database, bus_id)
    try:
        writer = dim.DimWriter(link, phm_id, lcd_id)
        print(f"WRITE dim-text on bus {bus_id}: PHM 0x{phm_id:08X}, LCD 0x{lcd_id:08X}")
        if args.disable:
            writer.disable()
            print("  sent LCD disable")
            return 0
        writer.enable()
        writer.show(args.text)
        print(f"  showed {args.text!r} (if the ids are right, it appears on the cluster)")
    finally:
        link.close()
    return 0


# ---- entry point --------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="volvo-monitor", description=__doc__.splitlines()[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transport", default="j2534",
                        choices=("j2534", "socketcan", "elm"))
    parser.add_argument("--library", help="path to the J2534 DLL (default: first registered)")
    parser.add_argument("--channel", default="can0", help="SocketCAN interface")
    parser.add_argument("--port", default="/dev/rfcomm0", help="ELM327 serial port")
    parser.add_argument("--baudrate", type=int, default=500_000, help="CAN bit rate")
    parser.add_argument("--elm-baud", type=int, default=38400, help="ELM327 serial baud")
    parser.add_argument("--ecu", default="ECM")
    parser.add_argument("--definitions", help="YAML file or directory (default: definitions/)")
    parser.add_argument("--profile", help="vehicle profile id to load (see `profiles`); "
                                          "optional when only one is present")
    parser.add_argument("--enable-writes", action="store_true",
                        help="allow the one supported write (clearing DTCs); off by default")
    parser.add_argument("-v", "--verbose", action="count", default=0)

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("devices", help="list registered J2534 drivers").set_defaults(func=cmd_devices)
    sub.add_parser("profiles", help="list available vehicle profiles").set_defaults(func=cmd_profiles)
    sub.add_parser("info", help="VIN, ECU identification, supported PIDs").set_defaults(
        func=cmd_info)
    sub.add_parser("scan", help="probe the standard OBD addresses").set_defaults(func=cmd_scan)

    probe = sub.add_parser("probe", help="check an ELM327 can do raw Volvo A6 (use --transport elm --port …)")
    probe.add_argument("--transport", default="elm", choices=("elm",),
                       help="only the ELM327 is probed")
    probe.add_argument("--port", default="/dev/rfcomm0",
                       help="ELM327 serial port (rfcomm on Linux, COMx on Windows)")
    probe.add_argument("--baud", type=int, default=38400, help="ELM327 serial baud (default 38400)")
    probe.set_defaults(func=cmd_probe)
    dtc = sub.add_parser("dtc", help="read stored trouble codes")
    dtc.add_argument("--clear", action="store_true",
                     help="clear codes after reading (a WRITE: sends AF 11 to each module)")
    dtc.set_defaults(func=cmd_dtc)
    sub.add_parser("identify", help="read identity/configuration (VIN, part numbers)").set_defaults(
        func=cmd_identify)
    sub.add_parser("params", help="list the loaded parameter definitions").set_defaults(
        func=cmd_params)

    config = sub.add_parser("config", help="read a module's programmed configuration (CEM)")
    config.add_argument("--config-map", help="config decode YAML (default: config-cem.yaml)")
    config.set_defaults(func=cmd_config)

    record = sub.add_parser("record", help="record parameters to a CSV over time")
    record.add_argument("file")
    record.add_argument("--interval", type=float, default=0.5)
    record.add_argument("--params", help="comma separated keys (default: the diesel/DPF set)")
    record.add_argument("--all", action="store_true", help="record every parameter for the ECU")
    record.set_defaults(func=cmd_record)

    analyze = sub.add_parser("analyze", help="post-drive report from a recorded trip CSV")
    analyze.add_argument("file")
    analyze.add_argument("--html", help="also write a self-contained HTML report with charts")
    analyze.set_defaults(func=cmd_analyze)

    dump = sub.add_parser("dump", help="back up a module's identity/config blocks to JSON")
    dump.add_argument("--ecu", default=argparse.SUPPRESS,
                      help="module name, e.g. DIM (also accepted before the subcommand)")
    dump.add_argument("--blocks", help="comma-separated hex block ids (default: FB,FC,F5)")
    dump.add_argument("--group", help="module comm address in hex, if --ecu is not defined")
    dump.add_argument("--bus",
                      help="CAN bus id from the vehicle profile (default: inferred from --ecu)")
    dump.add_argument("--out", help="output file (default: dump-<ecu>-<timestamp>.json)")
    dump.set_defaults(func=cmd_dump)

    diff = sub.add_parser("diff", help="compare two dumps (byte + decoded changes)")
    diff.add_argument("before")
    diff.add_argument("after")
    diff.set_defaults(func=cmd_diff)

    rm = sub.add_parser("read-mem", help="EXPERIMENTAL read-only memory probe (0xBB by address)")
    rm.add_argument("--ecu", default=argparse.SUPPRESS,
                    help="module name, e.g. CEM (also accepted before the subcommand)")
    rm.add_argument("--group", help="module comm address in hex, if --ecu is not defined")
    rm.add_argument("--bus", help="CAN bus id (default: inferred from --ecu)")
    rm.add_argument("--service", default="BB", help="read service byte in hex (default BB)")
    rm.add_argument("--addr", help="start address in hex (e.g. 0x4000)")
    rm.add_argument("--addr-bytes", type=int, default=3, help="address width in bytes (default 3)")
    rm.add_argument("--len", type=int, default=6, help="bytes to request (default 6)")
    rm.add_argument("--raw", help="send this hex A6 payload verbatim instead (e.g. 50BB004000 06)")
    rm.add_argument("--timeout", type=float, default=1.0)
    rm.set_defaults(func=cmd_readmem)

    sn = sub.add_parser("sniff", help="passively dump CAN frames on a bus (read-only)")
    sn.add_argument("--bus", help="CAN bus id (default: the primary/500k bus; use ls for 125k)")
    sn.add_argument("--id", help="watch only this 29-bit CAN id (hex)")
    sn.add_argument("--duration", type=float, default=15.0, help="seconds to listen (default 15)")
    sn.add_argument("--all", action="store_true", help="print every frame, not just changed payloads")
    sn.add_argument("--out", help="save the capture to a file (for sniff-diff)")
    sn.set_defaults(func=cmd_sniff)

    sd = sub.add_parser("sniff-diff", help="find the byte that changed between two sniff captures")
    sd.add_argument("before")
    sd.add_argument("after")
    sd.set_defaults(func=cmd_sniffdiff)

    dt = sub.add_parser("dim-text", help="EXPERIMENTAL write: show text on the cluster (DIM)")
    dt.add_argument("text", nargs="?", default="", help="up to 32 chars to display")
    dt.add_argument("--year", choices=("2001", "2002", "facelift"),
                    help="model-year preset for the phone/LCD ids (V50 2007 = facelift)")
    dt.add_argument("--phm-id", help="phone/message module CAN id in hex (overrides --year)")
    dt.add_argument("--lcd-id", help="DIM screen-control CAN id in hex (overrides --year)")
    dt.add_argument("--bus", help="CAN bus id (default: the cabin/125k bus)")
    dt.add_argument("--disable", action="store_true", help="send the LCD-disable frame and exit")
    dt.set_defaults(func=cmd_dimtext)

    read = sub.add_parser("read", help="read one parameter key or one raw request")
    read.add_argument("what", help="a parameter key (boost_actual) or hex (22F190)")
    read.set_defaults(func=cmd_read)

    monitor = sub.add_parser("monitor", help="live table of parameters")
    monitor.add_argument("--interval", type=float, default=0.5)
    monitor.add_argument("--csv", help="also append every sample to this file")
    monitor.add_argument("--params", help="comma separated parameter keys to show")
    monitor.add_argument("--all", action="store_true",
                         help="show every parameter defined for the ECU")
    monitor.add_argument("--no-group", action="store_true",
                         help="flat table instead of grouped sections")
    monitor.add_argument("--once", action="store_true")
    monitor.set_defaults(func=cmd_monitor)

    serve = sub.add_parser("serve", help="live dashboard as a local web page")
    serve.add_argument("--interval", type=float, default=0.2,
                       help="min seconds between poll cycles (polls never overlap; "
                            "lower = livelier, e.g. 0.1)")
    serve.add_argument("--fake", action="store_true",
                       help="serve synthetic data (preview the page without a car)")
    serve.add_argument("--host", default="127.0.0.1",
                       help="bind address (0.0.0.0 to reach it from the host browser)")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--elm-port", default="/dev/rfcomm0",
                       help="ELM327 serial port (with --transport elm)")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=[logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)],
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        return args.func(args)
    except TransportError as exc:
        print(f"transport error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
