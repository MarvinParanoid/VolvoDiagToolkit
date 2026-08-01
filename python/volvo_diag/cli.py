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
from .volvo.vehicle import DASHBOARD_KEYS, EngineState, Vehicle

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
    """Uniform 'give me an EngineState' front for the monitor, hiding whether
    the ECM speaks UDS (Vehicle over a Transport) or the Volvo A6 protocol
    (VolvoEcm over a raw-CAN link)."""

    def __init__(self, description: str, sample, close) -> None:
        self.description = description
        self.sample = sample  # (keys) -> EngineState
        self._close = close

    def __enter__(self) -> "_Reader":
        return self

    def __exit__(self, *exc: object) -> None:
        self._close()


def open_reader(args: argparse.Namespace, database: pdb.Database | None) -> _Reader:
    if ecm_is_volvo(database) and args.transport == "j2534":
        from .transport.j2534 import J2534CanLink
        from .transport.volvo_ecm import VolvoEcm
        from .volvo.vehicle import engine_state_via_volvo

        link = J2534CanLink(args.library, baudrate=args.baudrate)
        link.open()
        ecm = VolvoEcm(link, group=database.ecus["ECM"].volvo_group)
        return _Reader(
            f"{link.describe()} (Volvo A6)",
            lambda keys: engine_state_via_volvo(ecm, database, keys),
            link.close,
        )

    transport = build_transport(args)
    transport.open()
    vehicle = Vehicle(transport, database)
    return _Reader(transport.describe(), vehicle.engine_state, transport.close)


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
    with build_transport(args) as transport:
        vehicle = Vehicle(transport, database)
        ecu = vehicle.ecu(args.ecu)

        target = args.what
        if database and target in database.parameters:
            parameter = database[target]
            reading = ecu.read(parameter)
            print(f"{parameter.name} [{parameter.status}]")
            print(f"  request   {parameter.request.hex().upper()}")
            print(f"  raw       {reading.raw.hex().upper() or '-'}")
            print(f"  value     {reading.error or parameter.format(reading.value)}")
            return 0 if reading.ok else 1

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


def render(state: EngineState, database: pdb.Database | None) -> str:
    def line(label: str, value, unit: str = "", note: str = "") -> str:
        text = "--" if value is None else (
            ("yes" if value else "no") if isinstance(value, bool) else f"{value:,.1f}"
        )
        return f"  {label:<28}{text:>10} {unit:<6} {note}"

    def status_of(key: str) -> str:
        if database is None or key not in database.parameters:
            return "(OBD-II PID)"
        parameter = database[key]
        return f"({parameter.status})"

    rows = [
        line("RPM", state.rpm, "rpm", status_of("rpm")),
        line("Boost actual", state.boost_kpa, "kPa", status_of("boost_actual")),
        line("Boost requested", state.boost_requested_kpa, "kPa", status_of("boost_requested")),
        line("DPF differential", state.dpf_pressure_kpa, "kPa",
             status_of("dpf_differential_pressure")),
        line("DPF soot load", state.soot_percent, "%", status_of("dpf_soot_load")),
        line("Exhaust temperature", state.exhaust_temperature_c, "degC",
             status_of("exhaust_temperature")),
        line("Regeneration", state.regeneration_active, "", status_of("regeneration_active")),
        line("Since regeneration", state.distance_since_regeneration_km, "km",
             status_of("distance_since_regeneration")),
    ]
    return "\n".join(rows)


def cmd_monitor(args: argparse.Namespace) -> int:
    database = load_database(args)
    keys = args.params.split(",") if args.params else list(DASHBOARD_KEYS)

    writer = None
    handle = None
    if args.csv:
        handle = Path(args.csv).open("w", newline="", encoding="utf-8")
        writer = csv.writer(handle)
        writer.writerow(["t", *keys])

    started = time.monotonic()
    try:
        with open_reader(args, database) as reader:
            print(f"{reader.description}   {len(keys)} parameters   ctrl-c to stop\n")
            first = True
            while True:
                state = reader.sample(keys)
                if not first:
                    print(f"\033[{10 + len(state.errors)}A", end="")
                first = False
                print(f"  t = {time.monotonic() - started:8.1f} s" + " " * 40)
                print(render(state, database) + " " * 10)
                for error in state.errors[:4]:
                    print(f"  ! {error[:90]:<92}")
                for _ in range(4 - min(4, len(state.errors))):
                    print(" " * 94)

                if writer:
                    values = {r.parameter.key: r.value for r in state.readings if r.ok}
                    writer.writerow([f"{time.monotonic() - started:.2f}",
                                     *[values.get(k, "") for k in keys]])
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
    sub.add_parser("params", help="list the loaded parameter definitions").set_defaults(
        func=cmd_params)

    read = sub.add_parser("read", help="read one parameter key or one raw request")
    read.add_argument("what", help="a parameter key (boost_actual) or hex (22F190)")
    read.set_defaults(func=cmd_read)

    monitor = sub.add_parser("monitor", help="live dashboard")
    monitor.add_argument("--interval", type=float, default=0.5)
    monitor.add_argument("--csv", help="also append every sample to this file")
    monitor.add_argument("--params", help="comma separated parameter keys")
    monitor.add_argument("--once", action="store_true")
    monitor.set_defaults(func=cmd_monitor)

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
