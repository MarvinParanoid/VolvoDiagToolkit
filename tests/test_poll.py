"""PollScheduler: the shared live-poll core. Live mode drips candidates on a
slow lane, backs off dead ids, and rides a miss on cache; faithful mode (record)
reads everything every cycle and reports a miss as a real gap."""

import unittest

from volvo_diag.poll import PollScheduler, read_timeout


class P:
    def __init__(self, key, status="verified"):
        self.key, self.status = key, status


def _cb(fail=()):
    fail = set(fail)
    log = []

    def read(param, timeout):
        log.append((param.key, timeout))
        if param.key in fail:
            raise RuntimeError("no answer")
        return 42.0
    return read, log


class LiveModeTest(unittest.TestCase):
    def test_candidate_slow_lane_then_cache(self):
        read, log = _cb()
        sch = PollScheduler(read, live=True)
        r1 = sch.cycle([P("c", "candidate")])
        r2 = sch.cycle([P("c", "candidate")])
        self.assertEqual([k for k, _ in log], ["c"])       # read once, not twice
        self.assertEqual(read_timeout("candidate"), 0.25)  # loose timeout used
        self.assertTrue(r1[0].fresh)
        self.assertTrue(r2[0].ok and not r2[0].fresh)      # served from cache

    def test_dead_id_backs_off(self):
        read, log = _cb(fail=["d"])
        sch = PollScheduler(read, live=True)
        for _ in range(4):
            sch.cycle([P("d", "verified")])
        self.assertEqual(sum(1 for k, _ in log if k == "d"), 3)  # 4th cycle backs off

    def test_miss_rides_last_value(self):
        read, log = _cb(fail=["p"])
        sch = PollScheduler(read, live=True)
        sch._last["p"] = {"value": 9.0, "t": 0.0}
        r = sch.cycle([P("p", "verified")])
        self.assertTrue(r[0].ok)            # keeps the last value
        self.assertGreater(r[0].age, 1.0)   # flagged stale


class FaithfulModeTest(unittest.TestCase):
    def test_reads_every_param_every_cycle_and_misses_are_gaps(self):
        read, log = _cb(fail=["b"])
        sch = PollScheduler(read, live=False)
        r1 = sch.cycle([P("a", "candidate"), P("b", "verified")])
        r2 = sch.cycle([P("a", "candidate"), P("b", "verified")])
        # both candidates AND verified read every cycle (no slow lane)
        self.assertEqual([k for k, _ in log], ["a", "b", "a", "b"])
        # a miss is a real gap, not a cached value
        self.assertFalse(r1[1].ok)
        self.assertFalse(r2[1].ok)
        self.assertTrue(r1[0].ok)
        self.assertEqual(sch.stats.selected, 2)


if __name__ == "__main__":
    unittest.main()
