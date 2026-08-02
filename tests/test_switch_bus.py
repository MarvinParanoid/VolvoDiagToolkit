"""VolvoBackend.switch_bus must be atomic: if opening the requested bus fails
(the 125k low-speed connect is driver-dependent and can be rejected), it rolls
back to the bus that was working so the dashboard keeps polling instead of
getting stuck on a closed link that answers every read with "link is not open"."""

import argparse
import unittest

from volvo_diag.backend import VolvoBackend
from volvo_diag.transport.base import TransportError
from volvo_diag.volvo.parameters import Bus


class FakeLink:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeDb:
    _buses = {"hs": Bus("hs", "hs", 500000, obd=True),
              "ls": Bus("ls", "ls", 125000, protocol=32772, obd=False)}

    def bus(self, bus_id):
        return self._buses[bus_id]   # KeyError for an unknown bus, like the real one


class SwitchBusTest(unittest.TestCase):
    def _backend(self, fails_on):
        # Build without __init__ so we can stub _open (no real J2534 device).
        b = VolvoBackend.__new__(VolvoBackend)
        b.args = argparse.Namespace(transport="j2534")
        b.db = FakeDb()
        b._bus = "hs"
        b._link = FakeLink()
        b._ecm = object()

        def fake_open():
            if b._bus in fails_on:
                raise TransportError(f"{b._bus} rejected by driver")
            b._link = FakeLink()
            b._ecm = object()

        b._open = fake_open
        return b

    def test_failed_switch_rolls_back_to_working_bus(self):
        b = self._backend(fails_on={"ls"})
        with self.assertRaises(TransportError):
            b.switch_bus("ls")
        self.assertEqual(b.current_bus(), "hs")   # rolled back
        self.assertIsNotNone(b._ecm)              # link still usable

    def test_successful_switch_changes_bus(self):
        b = self._backend(fails_on=set())
        b.switch_bus("ls")
        self.assertEqual(b.current_bus(), "ls")
        self.assertIsNotNone(b._ecm)

    def test_both_buses_dead_leaves_no_link(self):
        b = self._backend(fails_on={"ls", "hs"})
        with self.assertRaises(TransportError):
            b.switch_bus("ls")
        self.assertIsNone(b._ecm)                 # nothing to fall back to


if __name__ == "__main__":
    unittest.main()
