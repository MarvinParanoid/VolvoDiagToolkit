"""ISO 14229 requests and responses, read side only.

Everything that writes to the car (0x2E, 0x27, 0x31, 0x2F, 0x11, 0x14) is
listed for log analysis but deliberately has no request builder: the client is
a reader until the parameter database is trustworthy.
"""

from __future__ import annotations

from dataclasses import dataclass

NEGATIVE_RESPONSE = 0x7F
POSITIVE_OFFSET = 0x40
RESPONSE_PENDING = 0x78

SERVICES = {
    0x10: "DiagnosticSessionControl",
    0x11: "ECUReset",
    0x14: "ClearDiagnosticInformation",
    0x19: "ReadDTCInformation",
    0x22: "ReadDataByIdentifier",
    0x23: "ReadMemoryByAddress",
    0x27: "SecurityAccess",
    0x28: "CommunicationControl",
    0x2A: "ReadDataByPeriodicIdentifier",
    0x2C: "DynamicallyDefineDataIdentifier",
    0x2E: "WriteDataByIdentifier",
    0x2F: "InputOutputControlByIdentifier",
    0x31: "RoutineControl",
    0x34: "RequestDownload",
    0x36: "TransferData",
    0x37: "RequestTransferExit",
    0x3E: "TesterPresent",
    0x85: "ControlDTCSetting",
}

NRC = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x14: "responseTooLong",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceedNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x72: "generalProgrammingFailure",
    0x78: "requestCorrectlyReceived-ResponsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}

SESSION_DEFAULT = 0x01
SESSION_PROGRAMMING = 0x02
SESSION_EXTENDED = 0x03

# Standard identifiers worth reading from any ECU before anything else.
DID_VIN = 0xF190
DID_ECU_SERIAL = 0xF18C
DID_SW_NUMBER = 0xF194
DID_HW_NUMBER = 0xF191
DID_SUPPLIER_ECU_SW = 0xF1A0
COMMON_IDENTIFIERS = {
    0xF186: "activeDiagnosticSession",
    0xF187: "vehicleManufacturerSparePartNumber",
    0xF188: "vehicleManufacturerECUSoftwareNumber",
    0xF189: "vehicleManufacturerECUSoftwareVersion",
    0xF18A: "systemSupplierIdentifier",
    0xF18C: "ECUSerialNumber",
    DID_HW_NUMBER: "vehicleManufacturerECUHardwareNumber",
    DID_SW_NUMBER: "vehicleManufacturerECUSoftwareNumber",
    DID_VIN: "VIN",
    0xF195: "systemSupplierECUSoftwareVersionNumber",
}


class NegativeResponse(Exception):
    def __init__(self, service: int, nrc: int) -> None:
        self.service = service
        self.nrc = nrc
        name = SERVICES.get(service, f"0x{service:02X}")
        reason = NRC.get(nrc, f"0x{nrc:02X}")
        super().__init__(f"{name} rejected: {reason} (0x{nrc:02X})")


class UnexpectedResponse(Exception):
    pass


def read_data_by_identifier(did: int) -> bytes:
    return bytes([0x22, (did >> 8) & 0xFF, did & 0xFF])


def read_dtc_by_status_mask(mask: int = 0xFF) -> bytes:
    return bytes([0x19, 0x02, mask])

def read_dtc_count(mask: int = 0xFF) -> bytes:
    return bytes([0x19, 0x01, mask])


def tester_present(suppress_response: bool = True) -> bytes:
    return bytes([0x3E, 0x80 if suppress_response else 0x00])


def diagnostic_session_control(session: int = SESSION_EXTENDED) -> bytes:
    return bytes([0x10, session])


def check(request: bytes, response: bytes) -> bytes:
    """Validates a response against its request and returns the payload after
    the echoed service and identifier."""
    if not response:
        raise UnexpectedResponse("empty response")

    service = request[0]
    if response[0] == NEGATIVE_RESPONSE:
        if len(response) < 3:
            raise UnexpectedResponse(f"truncated negative response: {response.hex()}")
        raise NegativeResponse(response[1], response[2])

    if response[0] != service + POSITIVE_OFFSET:
        raise UnexpectedResponse(
            f"response to service 0x{service:02X} came back as 0x{response[0]:02X}"
        )

    if service == 0x22:
        if len(response) < 3 or response[1:3] != request[1:3]:
            raise UnexpectedResponse(
                f"asked for {request[1:3].hex().upper()}, got {response[1:3].hex().upper()}"
            )
        return response[3:]
    if service in (0x10, 0x19, 0x3E, 0x11, 0x85, 0x28):
        return response[2:] if len(response) > 1 else b""
    return response[1:]


@dataclass
class Dtc:
    code: int  # 3-byte DTC as stored
    status: int

    @property
    def text(self) -> str:
        """The P/C/B/U form. Volvo's own numbering differs; this is the ISO one."""
        first = (self.code >> 16) & 0xFF
        letter = "PCBU"[(first >> 6) & 0x03]
        return f"{letter}{(first >> 4) & 0x03}{first & 0x0F:X}{(self.code >> 8) & 0xFF:02X}" \
               f"-{self.code & 0xFF:02X}"

    @property
    def confirmed(self) -> bool:
        return bool(self.status & 0x08)

    @property
    def pending(self) -> bool:
        return bool(self.status & 0x04)


def parse_dtcs(payload: bytes) -> list[Dtc]:
    """Parses the body of a 0x19 0x02 response (availability mask stripped)."""
    dtcs = []
    for offset in range(0, len(payload) - 3, 4):
        code = int.from_bytes(payload[offset : offset + 3], "big")
        if code == 0:
            continue
        dtcs.append(Dtc(code=code, status=payload[offset + 3]))
    return dtcs
