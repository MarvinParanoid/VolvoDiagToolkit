"""Talking to one ECU."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..protocol import obd, uds
from ..transport.base import EcuAddress, Transport, TransportError
from .parameters import DecodeError, Database, Parameter

log = logging.getLogger(__name__)


@dataclass
class Reading:
    parameter: Parameter
    value: Any = None
    raw: bytes = b""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def suspect(self) -> bool:
        """Answered, but outside the range the definition promises."""
        return self.ok and not self.parameter.in_range(self.value)

    def __str__(self) -> str:
        if self.error:
            return f"{self.parameter.name}: {self.error}"
        return f"{self.parameter.name}: {self.parameter.format(self.value)}"


class Ecu:
    """A UDS/OBD conversation with one address.

    Read-only by design: nothing here writes to the car, clears adaptations or
    starts a routine. See docs/method.md for why that stays true until the
    parameter database is verified.
    """

    def __init__(
        self,
        transport: Transport,
        address: EcuAddress,
        database: Database | None = None,
        *,
        timeout: float = 1.0,
    ) -> None:
        self.transport = transport
        self.address = address
        self.database = database
        self.timeout = timeout

    # ---- raw ------------------------------------------------------------

    def request(self, payload: bytes, timeout: float | None = None) -> bytes:
        return self.transport.request(self.address, payload, timeout or self.timeout)

    def read_did(self, did: int) -> bytes:
        request = uds.read_data_by_identifier(did)
        return uds.check(request, self.request(request))

    # ---- identification --------------------------------------------------

    def vin(self) -> str:
        """Tries UDS 22 F190 first, then OBD 09 02."""
        try:
            return self.read_did(uds.DID_VIN).decode("ascii", errors="replace").strip("\x00 ")
        except (uds.NegativeResponse, uds.UnexpectedResponse, TransportError) as exc:
            log.debug("22F190 failed (%s), trying mode 09", exc)
        request = obd.vin_request()
        return obd.parse_vin(obd.parse(0x02, self.request(request), obd.MODE_VEHICLE_INFO))

    def identify(self) -> dict[str, str]:
        """Everything the ECU will tell us about itself, best effort."""
        out: dict[str, str] = {}
        for did, name in uds.COMMON_IDENTIFIERS.items():
            try:
                data = self.read_did(did)
            except (uds.NegativeResponse, uds.UnexpectedResponse, TransportError):
                continue
            text = data.decode("ascii", errors="replace").strip("\x00 ")
            printable = sum(c.isprintable() for c in text) >= max(1, len(text) - 1)
            out[name] = text if printable and text else data.hex().upper()
        return out

    # ---- diagnostics -----------------------------------------------------

    def dtcs(self, mask: int = 0xFF) -> list[uds.Dtc]:
        request = uds.read_dtc_by_status_mask(mask)
        body = uds.check(request, self.request(request))
        return uds.parse_dtcs(body[1:])  # first byte is the availability mask

    def supported_pids(self) -> list[int]:
        found: list[int] = []
        for base in obd.SUPPORT_PIDS:
            try:
                data = obd.parse(base, self.request(obd.request(base)))
            except (ValueError, TransportError):
                break
            if len(data) < 4:
                break
            found.extend(obd.supported_pids(base, data))
            if base + 0x20 not in found:
                break
        return found

    def read_pid(self, pid: int) -> float:
        data = obd.parse(pid, self.request(obd.request(pid)))
        return obd.decode(pid, data)

    # ---- database driven -------------------------------------------------

    def read(self, parameter: Parameter) -> Reading:
        try:
            response = self.request(parameter.request)
        except TransportError as exc:
            return Reading(parameter, error=str(exc))

        if len(response) >= 3 and response[0] == 0x7F:
            return Reading(
                parameter,
                raw=response,
                error=uds.NRC.get(response[2], f"NRC 0x{response[2]:02X}"),
            )
        try:
            return Reading(parameter, value=parameter.decode(response), raw=response)
        except DecodeError as exc:
            return Reading(parameter, raw=response, error=str(exc))

    def read_all(self, parameters: list[Parameter]) -> list[Reading]:
        return [self.read(p) for p in parameters]


def open_ecu(
    transport: Transport,
    database: Database | None,
    name: str = "ECM",
    *,
    fallback: EcuAddress | None = None,
) -> Ecu:
    """Looks the address up in the database, falling back to standard OBD."""
    definition = database.ecus.get(name.upper()) if database else None
    if definition is not None:
        address = EcuAddress(tx_id=definition.tx_id, rx_id=definition.rx_id, name=definition.name)
    elif fallback is not None:
        address = fallback
    else:
        address = EcuAddress.obd(name=name)
    return Ecu(transport, address, database)
