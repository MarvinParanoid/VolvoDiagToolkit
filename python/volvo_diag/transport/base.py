"""The one interface everything above this line is written against.

A transport moves a diagnostic payload to an ECU address and brings the answer
back. Segmentation may happen in the adapter (J2534, ELM327) or in software
(raw SocketCAN); above this line it makes no difference.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EcuAddress:
    """A request/response CAN id pair."""

    tx_id: int
    rx_id: int
    extended: bool = False  # 29-bit identifiers
    name: str = ""

    @classmethod
    def obd(cls, index: int = 0, name: str = "") -> "EcuAddress":
        """The standard 11-bit physical addressing pair: 0x7E0/0x7E8..."""
        return cls(tx_id=0x7E0 + index, rx_id=0x7E8 + index, name=name)

    def __str__(self) -> str:
        label = f"{self.name} " if self.name else ""
        return f"{label}{self.tx_id:03X}/{self.rx_id:03X}"


class TransportError(Exception):
    pass


class TransportTimeout(TransportError):
    pass


class Transport(ABC):
    """Blocking request/response over one CAN bus.

    Implementations must be usable as a context manager and must tolerate
    ``open()`` being called twice.
    """

    #: Set by implementations that cannot address more than one ECU at a time.
    single_target: bool = False

    def __enter__(self) -> "Transport":
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @abstractmethod
    def open(self) -> None:
        ...

    @abstractmethod
    def close(self) -> None:
        ...

    @abstractmethod
    def request(self, address: EcuAddress, payload: bytes, timeout: float = 1.0) -> bytes:
        """Sends one diagnostic payload and returns the assembled answer.

        Must transparently wait out ``7F xx 78`` (responsePending) replies and
        raise ``TransportTimeout`` if nothing usable arrives.
        """

    def describe(self) -> str:
        return self.__class__.__name__


def wait_out_pending(
    read_again,
    response: bytes,
    *,
    deadline_checker,
    limit: int = 20,
) -> bytes:
    """Shared handling of ``7F <sid> 78``.

    An ECU that is busy — a DPF regeneration request, a long DID read — answers
    responsePending repeatedly until the real answer is ready.
    """
    attempts = 0
    while (
        len(response) >= 3
        and response[0] == 0x7F
        and response[2] == 0x78
        and attempts < limit
        and deadline_checker()
    ):
        attempts += 1
        log.debug("responsePending (%d), waiting for the real answer", attempts)
        response = read_again()
    return response
