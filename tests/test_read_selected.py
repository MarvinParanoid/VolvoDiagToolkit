"""The live poll (VolvoBackend.read_selected) must stay snappy: trusted ids read
every cycle with a tight timeout, unconfirmed (candidate) ids drop to a slow
lane, dead ids back off, and each cycle reports timing stats — so a few
non-answering DIDs can't drag the dashboard."""

import argparse
import unittest

from volvo_diag.backend import VolvoBackend
from volvo_diag.transport.base import TransportError


class FakeParam:
    def __init__(self, key, status, ecu="ECM", name="X", unit=""):
        self.key, self.status, self.ecu, self.name, self.unit = key, status, ecu, name, unit

    def format(self, value, with_unit=True):
        return str(value)


class FakeDb:
    def __init__(self, *params):
        self.parameters = {p.key: p for p in params}


class FakeEcm:
    def __init__(self, fail=()):
        self.fail = set(fail)
        self.reads = []
        self.timeouts = {}

    def read(self, p, timeout=None):
        self.reads.append(p.key)
        self.timeouts[p.key] = timeout
        if p.key in self.fail:
            raise TransportError("no answer")
        return 42.0


def _backend(db, ecm):
    b = VolvoBackend.__new__(VolvoBackend)
    b.args = argparse.Namespace(transport="j2534")
    b.db = db
    b._ecm = ecm
    b._miss, b._last, b._slow_next, b._stats, b._poll = {}, {}, {}, {}, 0
    return b


class ReadSelectedTest(unittest.TestCase):
    def test_verified_reads_every_cycle_with_stats(self):
        ecm = FakeEcm()
        b = _backend(FakeDb(FakeParam("rpm", "verified")), ecm)
        rows = b.read_selected(["rpm"])
        b.read_selected(["rpm"])
        self.assertEqual(ecm.reads, ["rpm", "rpm"])          # fast lane, every cycle
        self.assertEqual(rows[0]["ok"], True)
        self.assertEqual(rows[0]["age"], 0.0)                # fresh
        self.assertEqual(ecm.timeouts["rpm"], 0.10)          # tight timeout for trusted
        self.assertEqual(b.last_stats()["selected"], 1)
        self.assertIn("cycle_ms", b.last_stats())

    def test_candidate_goes_to_slow_lane(self):
        ecm = FakeEcm()
        b = _backend(FakeDb(FakeParam("cand", "candidate")), ecm)
        b.read_selected(["cand"])   # first cycle: due (never polled) -> reads
        b.read_selected(["cand"])   # immediately after: not due (<3s) -> cached, no read
        self.assertEqual(ecm.reads, ["cand"])               # polled once, not twice
        self.assertEqual(ecm.timeouts["cand"], 0.25)        # looser timeout for candidate

    def test_dead_param_backs_off(self):
        ecm = FakeEcm(fail=["dead"])
        b = _backend(FakeDb(FakeParam("dead", "verified")), ecm)
        for _ in range(4):
            b.read_selected(["dead"])
        # cycles 1-3 read (and miss); by cycle 4 (misses>=3) it's backed off
        self.assertEqual(ecm.reads.count("dead"), 3)
        self.assertEqual(b.last_stats()["timeouts"], 0)      # cycle 4 didn't even try

    def test_cached_value_ages_when_not_read(self):
        ecm = FakeEcm(fail=["p"])
        b = _backend(FakeDb(FakeParam("p", "verified")), ecm)
        b._last["p"] = {"value": "9", "num": 9.0, "t": 0.0}  # a very old good read
        rows = b.read_selected(["p"])
        self.assertEqual(rows[0]["ok"], True)                # keeps the last value
        self.assertGreater(rows[0]["age"], 1.0)              # but flagged stale


if __name__ == "__main__":
    unittest.main()
