"""Parameter definitions, loaded from YAML rather than compiled in.

A definition is a claim about a specific ECU software level, so each one keeps
its provenance: where it came from, how confident we are, and a raw sample it
was derived from. Nothing here is invented — a parameter only enters the
database once a log shows the car answering it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import yaml

#: How much a definition can be trusted, worst to best.
STATUS_ORDER = ("candidate", "discovered", "experimental", "verified", "verified-against-vida")


class DefinitionError(Exception):
    pass


class DecodeError(Exception):
    pass


@dataclass(frozen=True)
class Encoding:
    type: str
    scale: float = 1.0
    offset: float = 0.0
    byte_offset: int = 0
    length: int | None = None
    bit: int | None = None
    values: dict[int, str] = field(default_factory=dict)

    _FIXED = {
        "uint8": (1, "B"),
        "int8": (1, "b"),
        "uint16_be": (2, ">H"),
        "int16_be": (2, ">h"),
        "uint16_le": (2, "<H"),
        "int16_le": (2, "<h"),
        "uint32_be": (4, ">I"),
        "int32_be": (4, ">i"),
    }

    @classmethod
    def parse(cls, raw: dict[str, Any] | None) -> "Encoding":
        raw = raw or {}
        kind = str(raw.get("type", "raw"))
        known = set(cls._FIXED) | {"raw", "ascii", "bit", "enum", "bool"}
        if kind not in known:
            raise DefinitionError(f"unknown encoding type {kind!r}")
        values = {int(k): str(v) for k, v in (raw.get("values") or {}).items()}
        return cls(
            type=kind,
            scale=float(raw.get("scale", 1.0)),
            offset=float(raw.get("offset", 0.0)),
            byte_offset=int(raw.get("byte_offset", 0)),
            length=raw.get("length"),
            bit=raw.get("bit"),
            values=values,
        )

    def size(self) -> int | None:
        if self.type in self._FIXED:
            return self._FIXED[self.type][0]
        if self.type in ("bit", "bool"):
            return 1
        return self.length

    def decode(self, data: bytes) -> Any:
        body = data[self.byte_offset :]
        if self.type == "raw":
            return body[: self.length] if self.length else body
        if self.type == "ascii":
            chunk = body[: self.length] if self.length else body
            return chunk.decode("ascii", errors="replace").strip("\x00 ")
        if self.type in ("bit", "bool"):
            if not body:
                raise DecodeError("no data")
            if self.type == "bool":
                return body[0] != 0
            return bool(body[0] & (1 << int(self.bit or 0)))

        if self.type == "enum":
            size = self.length or 1
            if len(body) < size:
                raise DecodeError(f"need {size} bytes, got {len(body)}")
            code = int.from_bytes(body[:size], "big")
            return self.values.get(code, f"unknown(0x{code:0{size * 2}X})")

        size, fmt = self._FIXED[self.type]
        if len(body) < size:
            raise DecodeError(f"need {size} bytes at offset {self.byte_offset}, got {len(body)}")
        (value,) = struct.unpack(fmt, body[:size])
        return value * self.scale + self.offset


@dataclass(frozen=True)
class Parameter:
    key: str
    ecu: str
    request: bytes
    response_prefix: bytes
    encoding: Encoding
    name: str = ""
    unit: str = ""
    status: str = "candidate"
    source: str = ""
    notes: str = ""
    minimum: float | None = None
    maximum: float | None = None
    sample: str = ""
    # "uds" (default, ISO15765 22/62) or "volvo" (the proprietary A6 read on
    # P1). A volvo parameter carries the bank and identifier instead of a UDS
    # request; see volvo_diag.protocol.volvo.
    protocol: str = "uds"
    group: int | None = None
    identifier: int | None = None

    @property
    def trusted(self) -> bool:
        return self.status in ("verified", "verified-against-vida")

    @property
    def is_volvo(self) -> bool:
        return self.protocol == "volvo"

    def matches(self, response: bytes) -> bool:
        return response.startswith(self.response_prefix)

    def strip(self, response: bytes) -> bytes:
        if not self.matches(response):
            raise DecodeError(
                f"{self.key}: expected a response starting with "
                f"{self.response_prefix.hex().upper()}, got {response[:8].hex().upper()}"
            )
        return response[len(self.response_prefix) :]

    def decode(self, response: bytes) -> Any:
        """Decodes a full UDS response (with the echoed service/DID)."""
        return self.encoding.decode(self.strip(response))

    def decode_value(self, value_bytes: bytes) -> Any:
        """Decodes already-extracted value bytes (the Volvo path hands these in
        after the protocol layer has stripped the frame)."""
        return self.encoding.decode(value_bytes)

    def in_range(self, value: Any) -> bool:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return True
        if self.minimum is not None and value < self.minimum:
            return False
        if self.maximum is not None and value > self.maximum:
            return False
        return True

    def format(self, value: Any) -> str:
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, float):
            text = f"{value:.1f}"
        elif isinstance(value, bytes):
            text = value.hex().upper()
        else:
            text = str(value)
        return f"{text} {self.unit}".strip()


@dataclass(frozen=True)
class EcuDefinition:
    name: str
    tx_id: int
    rx_id: int
    description: str = ""
    network: str = ""
    protocol: str = "uds"          # "uds" or "volvo"
    volvo_group: int = 0x11        # default bank for the Volvo A6 read

    @property
    def is_volvo(self) -> bool:
        return self.protocol == "volvo"


@dataclass
class Database:
    vehicle: dict[str, Any] = field(default_factory=dict)
    ecus: dict[str, EcuDefinition] = field(default_factory=dict)
    parameters: dict[str, Parameter] = field(default_factory=dict)
    sources: list[Path] = field(default_factory=list)

    def __iter__(self) -> Iterator[Parameter]:
        return iter(self.parameters.values())

    def __len__(self) -> int:
        return len(self.parameters)

    def __getitem__(self, key: str) -> Parameter:
        try:
            return self.parameters[key]
        except KeyError:
            raise KeyError(f"no parameter {key!r} in {[str(p) for p in self.sources]}") from None

    def for_ecu(self, ecu: str) -> list[Parameter]:
        return [p for p in self.parameters.values() if p.ecu.upper() == ecu.upper()]

    def select(self, keys: list[str] | None = None, *, min_status: str = "candidate",
               ecu: str | None = None) -> list[Parameter]:
        floor = STATUS_ORDER.index(min_status)
        chosen = []
        for parameter in self.parameters.values():
            if keys and parameter.key not in keys:
                continue
            if ecu and parameter.ecu.upper() != ecu.upper():
                continue
            rank = STATUS_ORDER.index(parameter.status) if parameter.status in STATUS_ORDER else 0
            if rank < floor:
                continue
            chosen.append(parameter)
        if keys:
            order = {key: index for index, key in enumerate(keys)}
            chosen.sort(key=lambda p: order.get(p.key, len(order)))
        return chosen

    def merge(self, other: "Database") -> None:
        self.vehicle.update(other.vehicle)
        self.ecus.update(other.ecus)
        self.parameters.update(other.parameters)
        self.sources.extend(other.sources)


def _hex(value: Any, field_name: str, key: str) -> bytes:
    if value is None:
        raise DefinitionError(f"{key}: missing {field_name}")
    text = str(value).replace(" ", "").replace("0x", "")
    try:
        return bytes.fromhex(text)
    except ValueError as exc:
        raise DefinitionError(f"{key}: {field_name} {value!r} is not hex") from exc


def _int(value: Any, field_name: str, key: str) -> int:
    if value is None:
        raise DefinitionError(f"{key}: missing {field_name}")
    try:
        return int(str(value), 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as exc:
        raise DefinitionError(f"{key}: {field_name} {value!r} is not an integer") from exc


def _default_response_prefix(request: bytes) -> bytes:
    """22 D1 23 is answered by 62 D1 23; 01 0C by 41 0C."""
    if not request:
        return b""
    service = request[0]
    if service in (0x22, 0x2C, 0x2E):
        return bytes([service + 0x40]) + request[1:3]
    if service in (0x01, 0x09, 0x21, 0x19):
        return bytes([service + 0x40]) + request[1:2]
    return bytes([service + 0x40])


def load_file(path: str | Path) -> Database:
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    database = Database(vehicle=raw.get("vehicle") or {}, sources=[path])

    for name, entry in (raw.get("ecus") or {}).items():
        database.ecus[name.upper()] = EcuDefinition(
            name=name.upper(),
            tx_id=int(entry["tx"]),
            rx_id=int(entry["rx"]),
            description=entry.get("description", ""),
            network=entry.get("network", ""),
            protocol=str(entry.get("protocol", "uds")).lower(),
            volvo_group=_int(entry.get("volvo_group", 0x11), "volvo_group", name),
        )

    for key, entry in (raw.get("parameters") or {}).items():
        entry = entry or {}
        status = str(entry.get("status", "candidate")).lower()
        if status not in STATUS_ORDER:
            raise DefinitionError(
                f"{key}: status {status!r} is not one of {', '.join(STATUS_ORDER)}"
            )
        protocol = str(entry.get("protocol", "uds")).lower()
        if protocol not in ("uds", "volvo"):
            raise DefinitionError(f"{key}: unknown protocol {protocol!r}")

        group = identifier = None
        if protocol == "volvo":
            group = _int(entry.get("group"), "group", key)
            identifier = _int(entry.get("identifier"), "identifier", key)
            # A volvo parameter has no UDS request; keep the fields empty so the
            # UDS path can never accidentally use them.
            request = b""
            prefix = b""
        else:
            request = _hex(entry.get("request"), "request", key)
            rp = entry.get("response_prefix")
            prefix = _hex(rp, "response_prefix", key) if rp else _default_response_prefix(request)

        database.parameters[key] = Parameter(
            key=key,
            ecu=str(entry.get("ecu", "ECM")).upper(),
            request=request,
            response_prefix=prefix,
            encoding=Encoding.parse(entry.get("encoding")),
            name=entry.get("name", key.replace("_", " ")),
            unit=entry.get("unit", ""),
            status=status,
            source=entry.get("source", ""),
            notes=entry.get("notes", ""),
            minimum=entry.get("min"),
            maximum=entry.get("max"),
            sample=entry.get("sample", ""),
            protocol=protocol,
            group=group,
            identifier=identifier,
        )

    return database


def load(*paths: str | Path) -> Database:
    """Loads one or more YAML files; later files win on conflicts."""
    database = Database()
    for path in paths:
        path = Path(path)
        files = sorted(path.rglob("*.yaml")) if path.is_dir() else [path]
        for item in files:
            database.merge(load_file(item))
    return database


def default_path() -> Path:
    """definitions/volvo as shipped in the repository.

    Deliberately not definitions/ — definitions/simulator holds fabricated
    identifiers for the fake driver and must never be loaded against a car.
    """
    return Path(__file__).resolve().parents[3] / "definitions" / "volvo"
