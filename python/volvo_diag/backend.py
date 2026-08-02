"""The dashboard backend: reads the car over the Volvo A6 link for `serve`.

Extracted from cli.py so the CLI stays about argument parsing and command glue.
`VolvoBackend` is a web.Backend that polls the selected parameters, switches CAN
buses, and reads configuration and trouble codes — all on the single adapter
thread web.serve drives.
"""

from __future__ import annotations

import argparse

from . import web
from .categories import category_for
from .volvo import parameters as pdb

# Live-poll tuning. Trusted ids answer in ~20 ms, so a tight timeout keeps a
# miss cheap; a candidate (unconfirmed) id may not answer at all, so it goes in a
# slow lane instead of burning the whole timeout every cycle.
_FAST_STATUS = {"verified-against-vida", "verified", "experimental", "discovered"}
_SLOW_INTERVAL = 3.0   # seconds: how rarely a candidate/unknown id is polled live


def _read_timeout(status: str) -> float:
    if status in ("verified-against-vida", "verified"):
        return 0.10
    if status in ("experimental", "discovered"):
        return 0.15
    return 0.25


def _open_bus_link(args: argparse.Namespace, bus):
    """Open a raw-CAN J2534 link for a Bus from the vehicle profile."""
    from .transport.j2534 import J2534CanLink

    link = J2534CanLink(args.library, baudrate=bus.baudrate, protocol=bus.protocol,
                        vendor_params=bus.vendor_params, sample_point=bus.sample_point)
    link.open()
    return link


def open_volvo_ecm(args: argparse.Namespace, database, bus_id: str | None = None):
    """Opens a raw-CAN VolvoEcm on a profile bus and returns (link, ecm).
    read_block/read take an explicit group per call."""
    from .transport.volvo_ecm import VolvoEcm

    bus = database.bus(bus_id) if bus_id else database.primary_bus()
    link = _open_bus_link(args, bus)
    default_group = database.ecus["ECM"].volvo_group if "ECM" in database.ecus else 0x11
    return link, VolvoEcm(link, group=default_group)


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
        self._bus = database.primary_bus().id
        self._link = None
        self._ecm = None
        self._miss: dict = {}   # key -> consecutive read misses (for poll back-off)
        self._last: dict = {}   # key -> {value, num, t} last good read (ride out misses)
        self._slow_next: dict = {}  # key -> monotonic time a slow/candidate key may poll again
        self._stats: dict = {}  # last poll-cycle metrics for the dashboard
        self._poll = 0
        self._open()

    def _open(self) -> None:
        from .transport.volvo_ecm import VolvoEcm

        group = self.db.ecus["ECM"].volvo_group if "ECM" in self.db.ecus else 0x11
        if self.args.transport == "elm":
            from .transport.elm_can import ElmCanLink
            elm_port = getattr(self.args, "elm_port", None) or self.args.port
            self._link = ElmCanLink(elm_port, baud=getattr(self.args, "elm_baud", 38400))
            self._link.open()
            self._ecm = VolvoEcm(self._link, group=group, timeout=1.0)  # ELM is slower
            return

        self._link = _open_bus_link(self.args, self.db.bus(self._bus))
        # A short read timeout keeps the poll snappy: the ECM answers in ~20 ms,
        # so a missed frame recovers next tick instead of stalling for a second.
        self._ecm = VolvoEcm(self._link, group=group, timeout=0.4)

    def description(self) -> str:
        lib = self._link.describe() if self._link else "J2534"
        return f"{lib} (Volvo A6) \u2014 {self.db.bus(self._bus).label}"

    def buses(self) -> list:
        # An ELM327 reaches only OBD-accessible buses (no vendor bus switch), so
        # offer just those; a J2534 device can switch every bus in the profile.
        buses = self.db.serve_buses()
        if self.args.transport == "elm":
            buses = [b for b in buses if b.obd]
        return [{"id": b.id, "label": b.label, "baudrate": b.baudrate} for b in buses]

    def current_bus(self) -> str:
        return self._bus

    def switch_bus(self, bus_id: str) -> None:
        from .transport.base import TransportError

        bus = self.db.bus(bus_id)  # validate before touching the link
        if self.args.transport == "elm" and not bus.obd:
            raise ValueError("ELM327 only reaches OBD-accessible buses")
        # Atomic: if the new bus fails to open (the 125k low-speed connect is
        # driver-dependent and may be rejected), roll back to the bus that was
        # working so the dashboard keeps polling instead of getting stuck on a
        # dead link that answers every read with "link is not open".
        prev = self._bus
        if self._link:
            self._link.close()
        self._bus = bus_id
        try:
            self._open()
        except Exception as exc:  # noqa: BLE001
            self._bus = prev
            try:
                self._open()  # reopen the previous, known-good bus
            except Exception:  # noqa: BLE001 — nothing left to fall back to
                self._link = self._ecm = None
            raise TransportError(f"could not open {bus_id} bus: {exc}") from exc

    def _bus_params(self) -> list:
        mods = set(self.db.bus(self._bus).modules)
        return sorted((p for p in self.db if p.is_volvo and p.ecu.upper() in mods),
                      key=lambda p: (p.ecu, p.identifier or 0))

    def list_params(self) -> list:
        out = []
        for p in self._bus_params():
            _rank, label = category_for(p)
            out.append({"key": p.key, "name": p.name, "unit": web._pretty_unit(p.unit),
                        "ecu": p.ecu, "status": p.status, "category": label})
        return out

    def last_stats(self) -> dict:
        return self._stats

    def _cached_row(self, key, p, label, now, error=""):
        """A row for a key not read this cycle: the last good value, aged so the
        page can dim it; or a miss row if we never got one."""
        last = self._last.get(key)
        if last is not None:
            return web._row(key, p.name, p.unit, p.ecu, p.status, label, True,
                            value=last["value"], num=last["num"],
                            age=round(now - last["t"], 2))
        return web._row(key, p.name, p.unit, p.ecu, p.status, label, False,
                        error=error or "no data yet")

    def read_selected(self, keys: list) -> list:
        from time import monotonic, perf_counter

        from .transport.base import TransportError

        if self._ecm is None:
            return [web._row(k, k, "", "", "error", "", False,
                             error="link is down — reconnect the adapter") for k in keys]
        self._poll += 1
        now = monotonic()
        cycle_start = perf_counter()
        rows = []
        timeouts = 0
        slow_done = False   # at most one candidate/slow read per cycle
        for key in keys:
            p = self.db.parameters.get(key)
            if p is None:
                continue
            _rank, label = category_for(p)
            slow = p.status not in _FAST_STATUS   # candidate/unconfirmed
            misses = self._miss.get(key, 0)
            # Back off an id that keeps missing (unreadable on this ECU): retry
            # only occasionally so it never stalls the good params.
            backed_off = misses >= 3 and self._poll % 12 != 0
            # A slow (candidate) id gets its own lane: at most one per cycle and
            # not more often than _SLOW_INTERVAL, so unknown DIDs can't drag the
            # live dashboard. Verified/experimental ids read every cycle.
            slow_due = slow and not slow_done and now >= self._slow_next.get(key, 0.0)
            if backed_off or (slow and not slow_due):
                rows.append(self._cached_row(key, p, label, now))
                continue
            if slow:
                slow_done = True
                self._slow_next[key] = now + _SLOW_INTERVAL
            try:
                value = self._ecm.read(p, timeout=_read_timeout(p.status))
                self._miss[key] = 0
                num = (round(float(value), 4)
                       if isinstance(value, (int, float)) and not isinstance(value, bool)
                       else None)
                # Unit is rendered separately (r.unit), so keep the value unit-less.
                vstr = p.format(value, with_unit=False)
                self._last[key] = {"value": vstr, "num": num, "t": monotonic()}
                rows.append(web._row(key, p.name, p.unit, p.ecu, p.status, label, True,
                                     value=vstr, num=num, age=0.0))
            except TransportError:
                self._miss[key] = misses + 1
                timeouts += 1
                rows.append(self._cached_row(key, p, label, now))
        cycle_ms = (perf_counter() - cycle_start) * 1000
        self._stats = {"cycle_ms": round(cycle_ms, 1),
                       "rate": round(1000 / cycle_ms, 1) if cycle_ms > 1 else None,
                       "selected": len(keys), "timeouts": timeouts}
        return rows

    def read_config(self) -> dict:
        from .transport.base import TransportError
        from .volvo import config as configmod

        if self._ecm is None:
            return {"error": "link is down — reconnect the adapter"}
        topo = self.db.config_topology()
        group = (self.db.ecus[topo.ecu].volvo_group
                 if topo.ecu in self.db.ecus else 0x50)
        # The configuration module answers its identity/config blocks on one
        # specific bus (the CEM on 500k here — silent on 125k, per the write-clock
        # capture). Switch to that bus regardless of the dashboard's current one,
        # then restore whatever was selected.
        prev = self._bus
        if self._bus != topo.bus:
            try:
                self.switch_bus(topo.bus)
            except Exception as exc:  # noqa: BLE001
                return {"error": f"configuration needs the {topo.bus} bus: {exc}"}
        try:
            cmap = configmod.load_map(profile_dir=self.db.profile_dir)
            # The live poll timeout (~0.4 s) is tuned for a fast single ECM read;
            # the identity/config blocks are large multi-frame answers routed
            # through the gateway and start later, so give them a generous window.
            cfg_timeout = 2.0

            def read_block_retry(ident, attempts=3):
                # A bus switch reopens the VXDIAG channel, and the first request
                # on a freshly opened channel is often dropped — retry a couple
                # of times before giving up.
                last = None
                for _ in range(attempts):
                    try:
                        return self._ecm.read_block(ident, group=group, timeout=cfg_timeout)
                    except TransportError as exc:
                        last = exc
                raise last

            identity, car = [], []
            try:
                raw_fb = read_block_retry(topo.identity_block)
                identity = [{"name": f.name, "value": f.value}
                            for f in configmod.decode_identity(raw_fb, cmap)]
            except TransportError as exc:
                return {"error": f"identity read failed: {exc}"}
            try:
                raw_fc = read_block_retry(topo.config_block)
                car = [{"name": o.name, "value": o.label, "raw": o.raw}
                       for o in configmod.decode_car_config(raw_fc, cmap)]
            except TransportError:
                car = []
            return {"identity": identity, "car_config": car}
        finally:
            if prev != topo.bus and self._bus == topo.bus:
                try:
                    self.switch_bus(prev)
                except Exception:  # noqa: BLE001
                    pass

    def read_dtcs(self) -> dict:
        from .transport.base import TransportError
        from .volvo import dtc as dtcmod

        if self._ecm is None:
            return {"error": "link is down — reconnect the adapter"}
        # Sweep the modules reachable on the current bus (ECM/ABS/CEM on 500k,
        # DIM/ICM/BPM/CEM on 125k); switch the bus to scan the other half.
        mods = set(self.db.bus(self._bus).modules)
        modules = sorted(((n, e.volvo_group) for n, e in self.db.ecus.items()
                          if e.is_volvo and n.upper() in mods), key=lambda m: m[1])
        cats: dict = {}
        out = []
        for name, group in modules:
            try:
                codes = self._ecm.read_dtcs(group=group)
            except TransportError:
                continue  # module silent on this bus
            cat = cats.setdefault(name, dtcmod.load_catalogue(name, self.db.profile_dir))
            for code in codes:
                out.append({"ecu": name, "code": f"{code:04X}",
                            "text": dtcmod.describe(code, cat) or "(not in catalogue)"})
        return {"bus": self._bus, "dtcs": out}

    def clear_dtcs(self) -> dict:
        """WRITE: clears stored codes on every module of the current bus (AF 11).
        Returns {cleared:[ecu], failed:[ecu]} or {error}."""
        from .transport.base import TransportError

        if self._ecm is None:
            return {"error": "link is down — reconnect the adapter"}
        mods = set(self.db.bus(self._bus).modules)
        modules = sorted(((n, e.volvo_group) for n, e in self.db.ecus.items()
                          if e.is_volvo and n.upper() in mods), key=lambda m: m[1])
        cleared, failed = [], []
        for name, group in modules:
            try:
                (cleared if self._ecm.clear_dtcs(group=group) else failed).append(name)
            except TransportError:
                failed.append(name)
        self._miss.clear()   # let backed-off params retry after a clear
        return {"bus": self._bus, "cleared": cleared, "failed": failed}

    def close(self) -> None:
        if self._link:
            self._link.close()
