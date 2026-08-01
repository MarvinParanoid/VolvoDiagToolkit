"""End to end: ctypes transport -> proxy -> fake driver -> JSONL -> analysis.

Skipped unless the C++ side has been built. On Linux that is

    cmake -S . -B build -G Ninja && cmake --build build
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from volvo_diag.logs import parser
from volvo_diag.logs.summarize import collect
from volvo_diag.transport.base import EcuAddress
from volvo_diag.volvo import parameters as pdb
from volvo_diag.volvo.ecm import Ecu
from volvo_diag.volvo.vehicle import Vehicle

REPO = Path(__file__).resolve().parents[1]
SUFFIX = ".dll" if os.name == "nt" else ".so"


def artifact(*candidates: str) -> Path | None:
    for pattern in candidates:
        matches = sorted(REPO.glob(pattern))
        if matches:
            return matches[0]
    return None


PROXY = artifact(f"build*/proxy/j2534proxy{SUFFIX}", f"build*/proxy/*/j2534proxy{SUFFIX}")
FAKE = artifact(f"build*/fake-j2534/fake_j2534{SUFFIX}", f"build*/fake-j2534/*/fake_j2534{SUFFIX}")


@unittest.skipUnless(PROXY and FAKE, "build the C++ targets first")
class ProxyIntegrationTest(unittest.TestCase):
    """The proxy reads its configuration and opens its log once per process,
    so the log directory has to be set up before the first call and shared by
    every test in the class."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = TemporaryDirectory()
        cls.logs = Path(cls._tmp.name)
        os.environ["VOLVO_J2534_REAL_DLL"] = str(FAKE)
        os.environ["VOLVO_J2534_LOG_DIR"] = str(cls.logs)
        os.environ["VOLVO_J2534_SESSION_TAG"] = "pytest"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def setUp(self) -> None:
        from volvo_diag.transport.j2534 import J2534Transport

        self.transport = J2534Transport(str(PROXY))
        self.transport.open()
        self.database = pdb.load(REPO / "definitions" / "simulator")
        self.vehicle = Vehicle(self.transport, self.database)

    def tearDown(self) -> None:
        self.transport.close()

    def test_version_and_battery(self):
        firmware, dll, api = self.transport.version()
        self.assertEqual(api, "04.04")
        self.assertIn("fake", dll)
        self.assertEqual(self.transport.battery_millivolts(), 14210)

    def test_vin_round_trip(self):
        self.assertEqual(self.vehicle.vin(), "YV1MW7546A2000001")

    def test_raw_request_keeps_leading_zero_bytes(self):
        """The CAN id in a PASSTHRU_MSG starts with two NUL bytes; a c_char
        buffer would silently truncate the whole message there."""
        response = self.vehicle.ecm.request(bytes.fromhex("22FE01"))
        self.assertEqual(response[:3].hex().upper(), "62FE01")
        self.assertEqual(len(response), 5)

    def test_negative_response_is_reported_not_raised_as_garbage(self):
        response = self.vehicle.ecm.request(bytes.fromhex("22DEAD"))
        self.assertEqual(response.hex().upper(), "7F2231")

    def test_dashboard_reads_every_simulated_parameter(self):
        state = self.vehicle.engine_state()
        self.assertFalse(state.is_empty())
        self.assertGreater(state.rpm, 0)
        self.assertGreater(state.boost_kpa, 50)
        self.assertIsNotNone(state.exhaust_temperature_c)
        self.assertIsInstance(state.regeneration_active, bool)
        self.assertEqual(state.errors, [])

    def test_dtcs(self):
        dtcs = self.vehicle.ecm.dtcs()
        self.assertEqual(len(dtcs), 2)
        self.assertTrue(all(dtc.confirmed for dtc in dtcs))

    def test_the_proxy_logged_what_we_sent(self):
        self.vehicle.ecm.request(bytes.fromhex("22FE03"))
        # flush_each is on by default, so the record is already on disk.

        logs = list(self.logs.glob("*.jsonl"))
        self.assertEqual(len(logs), 1, f"expected one log, got {logs}")
        log = parser.load(logs[0])
        self.assertEqual(log.session.get("tag"), "pytest")

        stats = collect(parser.pair(log.frames))
        keys = {key for (_, _, key) in stats}
        self.assertIn("22FE03", keys)
        entry = next(v for k, v in stats.items() if k[2] == "22FE03")
        self.assertEqual(entry.can_id, 0x7E0)
        self.assertEqual(entry.answered, 1)
        self.assertTrue(entry.example_response.startswith("62FE03"))

    def test_survives_a_driver_that_times_out_with_a_message_in_hand(self):
        """A real VXDIAG returns ERR_TIMEOUT from PassThruReadMsgs while having
        already written a TX_DONE indication, and sometimes the answer, into
        the buffer. Treating a non-zero code as fatal loses both."""
        os.environ["VOLVO_FAKE_QUIRK"] = "vxdiag"
        try:
            ecu = Ecu(self.transport, EcuAddress.obd(name="ECM"), self.database, timeout=0.3)
            response = ecu.request(bytes.fromhex("22FE01"))
            self.assertEqual(response[:3].hex().upper(), "62FE01")
            self.assertEqual(len(response), 5)

            # The TX_DONE indication carries the CAN id and no payload; it must
            # never be mistaken for the ECU's answer.
            vin = ecu.request(bytes.fromhex("22F190"))
            self.assertTrue(vin.startswith(bytes.fromhex("62F190")))
        finally:
            os.environ.pop("VOLVO_FAKE_QUIRK", None)

    def test_filters_are_created_once_per_address(self):
        address = EcuAddress.obd(name="ECM")
        for _ in range(3):
            self.transport.request(address, bytes.fromhex("3E00"))
        self.assertEqual(len(self.transport._filters), 1)


@unittest.skipUnless(PROXY and FAKE, "build the C++ targets first")
class VolvoProtocolIntegrationTest(unittest.TestCase):
    """The raw-CAN Volvo A6 read path, through the proxy into the fake driver,
    which emulates the protocol found on the real D4164T."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = TemporaryDirectory()
        os.environ["VOLVO_J2534_REAL_DLL"] = str(FAKE)
        os.environ["VOLVO_J2534_LOG_DIR"] = cls._tmp.name
        os.environ["VOLVO_J2534_SESSION_TAG"] = "pytest-volvo"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def setUp(self) -> None:
        from volvo_diag.transport.j2534 import J2534CanLink
        from volvo_diag.transport.volvo_ecm import VolvoEcm

        self.link = J2534CanLink(str(PROXY))
        self.link.open()
        self.ecm = VolvoEcm(self.link)
        self.db = pdb.load(REPO / "definitions" / "volvo" / "p1" / "d4164t.yaml")

    def tearDown(self) -> None:
        self.link.close()

    def test_reads_a_raw_identifier_over_raw_can(self):
        raw = self.ecm.read_identifier(0x3A)
        self.assertEqual(len(raw), 2)  # a 16-bit value

    def test_reads_the_boost_and_dpf_parameters(self):
        from volvo_diag.volvo.vehicle import engine_state_via_volvo

        state = engine_state_via_volvo(self.ecm, self.db)
        self.assertEqual(state.errors, [])
        self.assertIsNotNone(state.boost_kpa)
        self.assertGreater(state.boost_kpa, 50)      # near/above atmospheric
        self.assertIsNotNone(state.dpf_pressure_kpa)

    def test_unknown_identifier_times_out(self):
        from volvo_diag.transport.base import TransportTimeout

        with self.assertRaises(TransportTimeout):
            self.ecm.read_identifier(0x4242, timeout=0.2)


if __name__ == "__main__":
    unittest.main()
