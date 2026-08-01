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

from .protocol import obd, uds
from .transport.base import EcuAddress, Transport, TransportError
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
    if args.transport == "vlinker":
        from .transport.vlinker import VLinkerTransport

        return VLinkerTransport(args.port)
    raise SystemExit(f"unknown transport {args.transport}")


def ecm_is_volvo(database: pdb.Database | None) -> bool:
    return database is not None and "ECM" in database.ecus and database.ecus["ECM"].is_volvo


class _Reader:
    """Reads individual parameters, hiding whether the ECM speaks UDS (Vehicle
    over a Transport) or the Volvo A6 protocol (VolvoEcm over a raw-CAN link).
    read_one(parameter) always returns a Reading."""

    def __init__(self, description: str, read_one, close, read_identity=None,
                 read_block=None) -> None:
        self.description = description
        self.read_one = read_one  # (Parameter) -> Reading
        self.read_identity = read_identity  # (group) -> list[str], or None (UDS)
        self.read_block = read_block  # (identifier, group) -> bytes, or None (UDS)
        self._close = close

    def __enter__(self) -> "_Reader":
        return self

    def __exit__(self, *exc: object) -> None:
        self._close()


def open_reader(args: argparse.Namespace, database: pdb.Database | None) -> _Reader:
    from .volvo.ecm import Reading

    if ecm_is_volvo(database) and args.transport == "j2534":
        from .transport.base import TransportError
        from .transport.j2534 import J2534CanLink
        from .transport.volvo_ecm import VolvoEcm

        link = J2534CanLink(args.library, baudrate=args.baudrate)
        link.open()
        ecm = VolvoEcm(link, group=database.ecus["ECM"].volvo_group)

        def read_one(parameter):
            try:
                return Reading(parameter, value=ecm.read(parameter))
            except TransportError as exc:
                return Reading(parameter, error=str(exc))

        return _Reader(f"{link.describe()} (Volvo A6)", read_one, link.close,
                       read_identity=ecm.read_identity, read_block=ecm.read_block)

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
    database = pdb.load(*(sorted(path.rglob("*.yaml")) if path.is_dir() else [path]))
    log.info("loaded %d parameters from %s", len(database), path)
    return database


# ---- commands -----------------------------------------------------------


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


def cmd_dtc(args: argparse.Namespace) -> int:
    database = load_database(args)
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


def _category(parameter) -> tuple:
    """A (sort order, label) for grouping a parameter in the dashboard. Derived
    from the ECU and name keywords - good enough to cluster the table without
    tagging every definition."""
    name = parameter.name.lower()
    unit = parameter.unit

    def kw(*words: str) -> bool:
        return any(w in name for w in words)

    if parameter.ecu.upper() == "CEM":
        if kw("relay", "light", "lamp", "beam", "signal", "wiper", "horn", "washer"):
            return (73, "CEM - outputs")
        if unit in ("V", "A") or kw("voltage", "current", "battery", "supply", "rheostat"):
            return (70, "CEM - electrical")
        if unit == "degC" or kw("temperature", "temp"):
            return (71, "CEM - climate")
        return (72, "CEM - other")

    if kw("boost", "manifold", "turbo", "charge", "intercool"):
        return (10, "Boost")
    if kw("particulate", "dpf", "exhaust", "regener"):
        return (11, "DPF & exhaust")
    if kw("fuel", "rail", "injection", "lambda"):
        return (12, "Fuel")
    if kw("egr"):
        return (13, "EGR")
    if kw("air mass", "mass air", "throttle", "pedal", "atmospher") or unit == "kg/h":
        return (14, "Air")
    if kw("coolant", "intake air temp") or unit == "degC":
        return (15, "Temperatures")
    if kw("speed", "rpm") or unit == "rpm":
        return (16, "Engine")
    return (19, "Other")


def group_readings(readings: list) -> list:
    """Groups readings into ordered (label, [reading]) sections, preserving the
    given order within each section."""
    order = {}
    for reading in readings:
        rank, label = _category(reading.parameter)
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
    """Serves the live dashboard as a local web page."""
    from . import web

    database = load_database(args)
    if database is None:
        print("no parameter database loaded", file=sys.stderr)
        return 1
    params = select_monitor_params(database, args)
    if not params:
        print("no parameters selected", file=sys.stderr)
        return 1

    with open_reader(args, database) as reader:
        web.serve(reader.description, reader.read_one, params, _category,
                  interval=args.interval, host=args.host, port=args.port)
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


def cmd_config(args: argparse.Namespace) -> int:
    """Reads a module's programmed configuration (read-only): the vehicle
    identity (0xFB), the car configuration options (0xFC) and, best-effort, the
    installed-modules map (0xC010). Decode maps come from CarCom; only the
    identity block is verified against a capture."""
    from .transport.base import TransportError
    from .volvo import config as configmod

    database = load_database(args)
    cmap = configmod.load_map(args.config_map)
    group = 0x50  # CEM comm address
    if database and "CEM" in database.ecus:
        group = database.ecus["CEM"].volvo_group

    with open_reader(args, database) as reader:
        if reader.read_block is None:
            print("configuration read needs the Volvo protocol (VXDIAG / J2534)")
            return 2

        try:
            raw_fb = reader.read_block(0xFB, group=group)
        except TransportError as exc:
            print(f"identity (0xFB): {exc}")
            return 1
        print("Vehicle identity (0xFB)")
        for field in configmod.decode_identity(raw_fb, cmap):
            print(f"  {field.name:<32} {field.value}")

        try:
            raw_fc = reader.read_block(0xFC, group=group)
        except TransportError as exc:
            print(f"\ncar configuration (0xFC): {exc}")
            return 0
        print(f"\nCar configuration (0xFC) — {len(raw_fc)} bytes  [unverified]")
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


# ---- entry point --------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="volvo-monitor", description=__doc__.splitlines()[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transport", default="j2534",
                        choices=("j2534", "socketcan", "vlinker"))
    parser.add_argument("--library", help="path to the J2534 DLL (default: first registered)")
    parser.add_argument("--channel", default="can0", help="SocketCAN interface")
    parser.add_argument("--port", default="/dev/rfcomm0", help="ELM327 serial port")
    parser.add_argument("--baudrate", type=int, default=500_000, help="CAN bit rate")
    parser.add_argument("--ecu", default="ECM")
    parser.add_argument("--definitions", help="YAML file or directory (default: definitions/)")
    parser.add_argument("-v", "--verbose", action="count", default=0)

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("devices", help="list registered J2534 drivers").set_defaults(func=cmd_devices)
    sub.add_parser("info", help="VIN, ECU identification, supported PIDs").set_defaults(
        func=cmd_info)
    sub.add_parser("scan", help="probe the standard OBD addresses").set_defaults(func=cmd_scan)
    sub.add_parser("dtc", help="read stored trouble codes").set_defaults(func=cmd_dtc)
    sub.add_parser("identify", help="read identity/configuration (VIN, part numbers)").set_defaults(
        func=cmd_identify)
    sub.add_parser("params", help="list the loaded parameter definitions").set_defaults(
        func=cmd_params)

    config = sub.add_parser("config", help="read a module's programmed configuration (CEM)")
    config.add_argument("--config-map", help="config decode YAML (default: config-cem.yaml)")
    config.set_defaults(func=cmd_config)

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
    serve.add_argument("--interval", type=float, default=0.5)
    serve.add_argument("--params", help="comma separated parameter keys to show")
    serve.add_argument("--all", action="store_true",
                       help="show every parameter defined for the ECU")
    serve.add_argument("--host", default="127.0.0.1",
                       help="bind address (0.0.0.0 to reach it from the host browser)")
    serve.add_argument("--port", type=int, default=8080)
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
