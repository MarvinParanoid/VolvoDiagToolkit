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
from .categories import categorize
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


# The dashboard's selectable buses. One CAN link at a time, so switching reopens
# it. The 500k powertrain bus reaches ECM, ABS and the CEM gateway; the low-speed
# cabin bus (VXDIAG protocol 32772 at 125k, with the vendor bus selector) reaches
# DIM and the other cabin modules that stay silent on 500k. CEM answers on both,
# so its configuration is read from whichever bus is up. Verified against a VIDA
# capture (which modules answer on which bus).
_SERVE_BUSES = [
    {"id": "hs", "label": "500k — ECM + ABS + CEM", "baudrate": 500000,
     "modules": ("ECM", "ABS", "CEM"), "protocol": 5, "vendor": {}, "sample_point": None},
    {"id": "ls", "label": "125k low-speed — DIM + CEM + cabin", "baudrate": 125000,
     "modules": ("DIM", "CEM", "ICM", "BPM"), "protocol": 32772,
     "vendor": {0x8001: 779}, "sample_point": 68},
]


def _bus_def(bus_id: str) -> dict:
    for bus in _SERVE_BUSES:
        if bus["id"] == bus_id:
            return bus
    raise ValueError(f"unknown bus {bus_id!r}")


def open_volvo_ecm(args: argparse.Namespace, bus_id: str = "hs"):
    """Opens a raw-CAN VolvoEcm on a serve-bus (hs = 500k, ls = 125k low-speed)
    and returns (link, ecm). read_block/read take an explicit group per call."""
    from .transport.j2534 import J2534CanLink
    from .transport.volvo_ecm import VolvoEcm

    bus = _bus_def(bus_id)
    link = J2534CanLink(args.library, baudrate=bus["baudrate"], protocol=bus["protocol"],
                        vendor_params=bus["vendor"], sample_point=bus["sample_point"])
    link.open()
    return link, VolvoEcm(link, group=0x11)


class VolvoBackend:
    """A web.Backend that reads the car over the Volvo A6 link, one CAN bus at a
    time. Switching bus reopens the J2534 link at the new baud rate.

    The VXDIAG J2534 driver only works on the thread that opened the device, so
    web.serve drives every adapter call from the main thread (the terminal
    monitor does the same and works). Do not call these methods from the HTTP
    or poller threads directly - web.serve marshals config/bus commands onto the
    main loop.
    """

    def __init__(self, args: argparse.Namespace, database: pdb.Database) -> None:
        self.args = args
        self.db = database
        self._bus = "hs"
        self._link = None
        self._ecm = None
        self._open()

    def _open(self) -> None:
        from .transport.j2534 import J2534CanLink
        from .transport.volvo_ecm import VolvoEcm

        bus = _bus_def(self._bus)
        self._link = J2534CanLink(self.args.library, baudrate=bus["baudrate"],
                                  protocol=bus["protocol"], vendor_params=bus["vendor"],
                                  sample_point=bus["sample_point"])
        self._link.open()
        group = self.db.ecus["ECM"].volvo_group if "ECM" in self.db.ecus else 0x11
        # A short read timeout keeps the poll snappy: the ECM answers in ~20 ms,
        # so a missed frame recovers next tick instead of stalling for a second.
        self._ecm = VolvoEcm(self._link, group=group, timeout=0.4)

    def description(self) -> str:
        lib = self._link.describe() if self._link else "J2534"
        return f"{lib} (Volvo A6) \u2014 {_bus_def(self._bus)['label']}"

    def buses(self) -> list:
        return [{"id": b["id"], "label": b["label"], "baudrate": b["baudrate"]}
                for b in _SERVE_BUSES]

    def current_bus(self) -> str:
        return self._bus

    def switch_bus(self, bus_id: str) -> None:
        _bus_def(bus_id)  # validate before touching the link
        if self._link:
            self._link.close()
        self._bus = bus_id
        self._open()

    def _bus_params(self) -> list:
        mods = set(_bus_def(self._bus)["modules"])
        return sorted((p for p in self.db if p.is_volvo and p.ecu.upper() in mods),
                      key=lambda p: (p.ecu, p.identifier or 0))

    def list_params(self) -> list:
        out = []
        for p in self._bus_params():
            _rank, label = _category(p)
            out.append({"key": p.key, "name": p.name, "unit": p.unit, "ecu": p.ecu,
                        "status": p.status, "category": label})
        return out

    def read_selected(self, keys: list) -> list:
        from .transport.base import TransportError

        rows = []
        for key in keys:
            p = self.db.parameters.get(key)
            if p is None:
                continue
            _rank, label = _category(p)
            try:
                value = self._ecm.read(p)
                num = (round(float(value), 4)
                       if isinstance(value, (int, float)) and not isinstance(value, bool)
                       else None)
                rows.append(web._row(key, p.name, p.unit, p.ecu, p.status, label, True,
                                     value=p.format(value), num=num))
            except TransportError as exc:
                rows.append(web._row(key, p.name, p.unit, p.ecu, p.status, label, False,
                                     error=str(exc)))
        return rows

    def read_config(self) -> dict:
        from .transport.base import TransportError
        from .volvo import config as configmod

        # CEM answers on both buses (it is the gateway), so configuration reads
        # from whichever one is up — no bus switch required.
        cmap = configmod.load_map()
        group = self.db.ecus["CEM"].volvo_group if "CEM" in self.db.ecus else 0x50
        identity, car = [], []
        try:
            raw_fb = self._ecm.read_block(0xFB, group=group)
            identity = [{"name": f.name, "value": f.value}
                        for f in configmod.decode_identity(raw_fb, cmap)]
        except TransportError as exc:
            return {"error": f"identity read failed: {exc}"}
        try:
            raw_fc = self._ecm.read_block(0xFC, group=group)
            car = [{"name": o.name, "value": o.label, "raw": o.raw}
                   for o in configmod.decode_car_config(raw_fc, cmap)]
        except TransportError:
            car = []
        return {"identity": identity, "car_config": car}

    def close(self) -> None:
        if self._link:
            self._link.close()


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
    """A (sort order, "Module · Subsystem") for grouping a parameter. See
    volvo_diag.categories."""
    return categorize(parameter.ecu, parameter.name, parameter.unit)


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
    """Serves the live dashboard as a local web page. Parameter selection, bus
    switching and the configuration view all live in the page itself."""
    if args.fake:
        backend = web.FakeBackend()
    else:
        database = load_database(args)
        if database is None:
            print("no parameter database loaded", file=sys.stderr)
            return 1
        if not (ecm_is_volvo(database) and args.transport == "j2534"):
            print("the live dashboard needs the Volvo protocol (VXDIAG / J2534); "
                  "use --fake to preview it without a car", file=sys.stderr)
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
    a JSON backup — a restore point to keep before ever changing anything, and a
    record of exactly what the module held. Read-only."""
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
    # low-speed modules (DIM and the cabin) only answer on the 125k bus
    bus_id = args.bus or ("ls" if ecu in _bus_def("ls")["modules"] else "hs")

    link, ecm = open_volvo_ecm(args, bus_id)
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
                for field in configmod.decode_identity(raw, configmod.load_map()):
                    if field.name == "VIN":
                        vin = field.value
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
    dump.add_argument("--blocks", help="comma-separated hex block ids (default: FB,FC,F5)")
    dump.add_argument("--group", help="module comm address in hex, if --ecu is not defined")
    dump.add_argument("--bus", choices=("hs", "ls"),
                      help="CAN bus: hs=500k, ls=125k low-speed (default: inferred from --ecu)")
    dump.add_argument("--out", help="output file (default: dump-<ecu>-<timestamp>.json)")
    dump.set_defaults(func=cmd_dump)

    diff = sub.add_parser("diff", help="compare two dumps (byte + decoded changes)")
    diff.add_argument("before")
    diff.add_argument("after")
    diff.set_defaults(func=cmd_diff)

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
    serve.add_argument("--fake", action="store_true",
                       help="serve synthetic data (preview the page without a car)")
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
