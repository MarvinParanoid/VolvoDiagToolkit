"""The top of the stack: ask the car a question, get a number.

Where a Volvo-specific definition exists it is used; where it does not, the
standard OBD-II PID is used instead and the reading says so. That way the
monitor is useful before a single DID has been reverse engineered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable

from ..protocol import obd
from ..transport.base import EcuAddress, Transport, TransportError
from .ecm import Ecu, Reading, open_ecu
from .parameters import Database, Parameter

log = logging.getLogger(__name__)

# The canonical keys the rest of the toolkit looks for. A definition file is
# free to add more; these are the ones the dashboard understands.
RPM = "rpm"
BOOST_ACTUAL = "boost_actual"
BOOST_REQUESTED = "boost_requested"
DPF_DIFFERENTIAL_PRESSURE = "dpf_differential_pressure"
DPF_SOOT_LOAD = "dpf_soot_load"
EXHAUST_TEMPERATURE = "exhaust_temperature"
REGENERATION_ACTIVE = "regeneration_active"
DISTANCE_SINCE_REGENERATION = "distance_since_regeneration"

DASHBOARD_KEYS = (
    RPM,
    BOOST_ACTUAL,
    BOOST_REQUESTED,
    DPF_DIFFERENTIAL_PRESSURE,
    DPF_SOOT_LOAD,
    EXHAUST_TEMPERATURE,
    REGENERATION_ACTIVE,
    DISTANCE_SINCE_REGENERATION,
)

#: Fallbacks used when the database has no verified definition for a key.
OBD_FALLBACKS = {
    RPM: 0x0C,
    BOOST_ACTUAL: 0x0B,
    EXHAUST_TEMPERATURE: 0x7C,
    DPF_DIFFERENTIAL_PRESSURE: 0x8B,
}

#: Addresses to try when looking for modules. Only the 0x7Ex block is
#: standardised; everything else has to be confirmed from a VIDA log before it
#: means anything (see docs/method.md, stage 7).
SCAN_ADDRESSES = tuple(EcuAddress.obd(i) for i in range(8))


@dataclass
class EngineState:
    """Mirrors the shape the Android layer is meant to receive."""

    rpm: float | None = None
    boost_kpa: float | None = None
    boost_requested_kpa: float | None = None
    dpf_pressure_kpa: float | None = None
    soot_percent: float | None = None
    exhaust_temperature_c: float | None = None
    regeneration_active: bool | None = None
    distance_since_regeneration_km: float | None = None
    readings: list[Reading] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return all(
            getattr(self, name) is None
            for name in (
                "rpm", "boost_kpa", "boost_requested_kpa", "dpf_pressure_kpa",
                "soot_percent", "exhaust_temperature_c", "regeneration_active",
            )
        )


class Vehicle:
    def __init__(self, transport: Transport, database: Database | None = None) -> None:
        self.transport = transport
        self.database = database
        self._ecus: dict[str, Ecu] = {}

    def ecu(self, name: str = "ECM") -> Ecu:
        key = name.upper()
        if key not in self._ecus:
            self._ecus[key] = open_ecu(self.transport, self.database, key)
        return self._ecus[key]

    @property
    def ecm(self) -> Ecu:
        return self.ecu("ECM")

    # ---- discovery -------------------------------------------------------

    def scan(self, addresses: Iterable[EcuAddress] = SCAN_ADDRESSES,
             timeout: float = 0.5) -> list[tuple[EcuAddress, str]]:
        """Finds which addresses answer at all. TesterPresent is the cheapest
        question that does not change anything in the car."""
        found = []
        for address in addresses:
            ecu = Ecu(self.transport, address, self.database, timeout=timeout)
            try:
                ecu.request(bytes([0x3E, 0x00]))
            except TransportError:
                continue
            label = ""
            try:
                label = ecu.read_did(0xF194).decode("ascii", "replace").strip("\x00 ")
            except Exception:  # noqa: BLE001 — identification is optional here
                pass
            found.append((address, label))
        return found

    # ---- readings --------------------------------------------------------

    def parameters(self, keys: Iterable[str] = DASHBOARD_KEYS) -> list[Parameter]:
        if self.database is None:
            return []
        return self.database.select(list(keys))

    def engine_state(self, keys: Iterable[str] = DASHBOARD_KEYS) -> EngineState:
        state = EngineState()
        wanted = list(keys)
        defined = {p.key: p for p in self.parameters(wanted)}
        ecm = self.ecm

        for key in wanted:
            parameter = defined.get(key)
            if parameter is not None:
                reading = ecm.read(parameter)
                state.readings.append(reading)
                if reading.ok:
                    self._apply(state, key, reading.value)
                    continue
                state.errors.append(f"{key}: {reading.error}")

            pid = OBD_FALLBACKS.get(key)
            if pid is None:
                continue
            try:
                self._apply(state, key, ecm.read_pid(pid))
            except (TransportError, ValueError, KeyError) as exc:
                state.errors.append(f"{key} via PID {pid:02X}: {exc}")
        return state

    @staticmethod
    def _apply(state: EngineState, key: str, value) -> None:
        mapping = {
            RPM: "rpm",
            BOOST_ACTUAL: "boost_kpa",
            BOOST_REQUESTED: "boost_requested_kpa",
            DPF_DIFFERENTIAL_PRESSURE: "dpf_pressure_kpa",
            DPF_SOOT_LOAD: "soot_percent",
            EXHAUST_TEMPERATURE: "exhaust_temperature_c",
            REGENERATION_ACTIVE: "regeneration_active",
            DISTANCE_SINCE_REGENERATION: "distance_since_regeneration_km",
        }
        attribute = mapping.get(key)
        if attribute is None:
            return
        if attribute == "regeneration_active" and not isinstance(value, bool):
            value = bool(value) if isinstance(value, (int, float)) else None
        setattr(state, attribute, value)

    # ---- convenience mirroring the architecture sketch --------------------

    def boost(self) -> float | None:
        return self.engine_state([BOOST_ACTUAL]).boost_kpa

    def dpf(self) -> float | None:
        return self.engine_state([DPF_DIFFERENTIAL_PRESSURE]).dpf_pressure_kpa

    def egt(self) -> float | None:
        return self.engine_state([EXHAUST_TEMPERATURE]).exhaust_temperature_c

    def vin(self) -> str:
        return self.ecm.vin()

    def supported_pids(self) -> list[int]:
        pids = self.ecm.supported_pids()
        for pid in pids:
            if pid in obd.PIDS:
                log.debug("supported: %02X %s", pid, obd.PIDS[pid].name)
        return pids
