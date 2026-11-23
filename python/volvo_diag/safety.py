"""The read-only boundary, in one place.

Every path that lets a *user-supplied* payload reach the bus (``read <hex>``, the
``read-mem`` probe, a parameter definition's ``request:``) checks it here first.
These are allowlists on purpose: a service byte that is not known to be a pure
read is refused, rather than trying to enumerate every write/control/security
service a module might implement.

The deliberate writes (``dtc --clear`` / the dashboard's clear, ``dim-text``) do
not go through here: they are built by dedicated code and gated behind
``--enable-writes`` instead.
"""

from __future__ import annotations

# UDS (ISO 14229) and OBD-II (SAE J1979) request service ids that only read.
# Notably absent: OBD 0x04 (clear DTCs), UDS 0x10 (session control), 0x11 (ECU
# reset), 0x14 (clear DTCs), 0x27 (security access), 0x28, 0x2C, 0x2E, 0x2F,
# 0x31, 0x34-0x37, 0x3D, 0x85, and KWP2000 0x3B (write by local id).
UDS_READ_SERVICES = frozenset({
    0x01,  # OBD: current data
    0x02,  # OBD: freeze-frame data
    0x03,  # OBD: stored DTCs
    0x05,  # OBD: O2 sensor test results (legacy, read)
    0x06,  # OBD: on-board monitoring results
    0x07,  # OBD: pending DTCs
    0x09,  # OBD: vehicle information (VIN, CALID)
    0x0A,  # OBD: permanent DTCs
    0x19,  # UDS: ReadDTCInformation
    0x1A,  # KWP2000: readEcuIdentification
    0x21,  # KWP2000: readDataByLocalIdentifier
    0x22,  # UDS: ReadDataByIdentifier
    0x3E,  # UDS: TesterPresent (keeps nothing but the current session alive)
})

# Volvo A6-family services (payload = [comm address][service][...]) that only
# read. See protocol/volvo.py for the whole family. Absent: A1 keep-alive, A3
# security, A8/A9 periodic transmission, AA dynamic define, AF clear DTC, B0/B1
# IO control, B2 routine, B4 define data, B8/BA write block.
VOLVO_READ_SERVICES = frozenset({
    0xA5, 0xA6, 0xA7,   # read data by offset / identifier / address
    0xAE,               # read DTC
    0xB9, 0xBB,         # read data block by offset / address
})


class UnsafeRequest(ValueError):
    """A payload that is not provably read-only."""


def check_uds_read(payload: bytes) -> None:
    """Raises UnsafeRequest unless `payload` is a UDS/OBD read request."""
    if not payload:
        raise UnsafeRequest("empty request")
    if payload[0] not in UDS_READ_SERVICES:
        raise UnsafeRequest(
            f"service 0x{payload[0]:02X} is not a known read-only UDS/OBD service; "
            "this toolkit does not send it")


def check_volvo_read(payload: bytes) -> None:
    """Raises UnsafeRequest unless `payload` ([comm][service]...) is a Volvo
    A6-family read request."""
    if len(payload) < 2:
        raise UnsafeRequest("a Volvo request needs at least [comm address][service]")
    if payload[1] not in VOLVO_READ_SERVICES:
        raise UnsafeRequest(
            f"service 0x{payload[1]:02X} is not a known read-only Volvo service; "
            "this toolkit does not send it")
