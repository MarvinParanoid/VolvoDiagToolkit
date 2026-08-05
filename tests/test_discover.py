"""discover.interpret / hints: candidate decodes for an unknown A6 value, used by
the `discover` sweep to hunt undocumented diesel-health ids."""

import unittest

from volvo_diag import discover


class InterpretTest(unittest.TestCase):
    def test_temperature_shape_matches_verified_dpf_temp(self):
        # 0x0E93 is the captured DPF-temp value on our car; decodes to ~100 C.
        c = discover.interpret(bytes.fromhex("0E93"))
        self.assertEqual(c["u16"], 0x0E93)
        self.assertEqual(c["temp"], 100.0)

    def test_single_byte_has_no_u16(self):
        c = discover.interpret(b"\x2a")
        self.assertEqual(c["u8"], 0x2A)
        self.assertNotIn("u16", c)

    def test_empty(self):
        self.assertEqual(discover.interpret(b""), {})


class HintsTest(unittest.TestCase):
    def test_soot_grams_range_flagged(self):
        self.assertIn("soot?g", discover.hints(bytes([0x00, 0x14])))  # 20 g

    def test_temperature_flagged(self):
        self.assertTrue(any("temp?" in h for h in discover.hints(bytes.fromhex("0E93"))))

    def test_absent_value_gives_no_hints(self):
        self.assertEqual(discover.hints(b"\x00\x00"), [])
        self.assertEqual(discover.hints(b"\xff\xff"), [])


class ClassifyTest(unittest.TestCase):
    def test_real_value_is_answered(self):
        self.assertEqual(discover.classify(bytes.fromhex("0E93"), 100.0), "answered")

    def test_all_ff_is_absent(self):
        self.assertEqual(discover.classify(b"\xff\xff", 0), "absent")

    def test_7fff_sentinel_is_absent(self):
        # the ECM answers 0x7FFF for "signal not available" (seen on lambda)
        self.assertEqual(discover.classify(b"\x7f\xff", 32767000.0), "absent")

    def test_collapsed_temperature_is_absent(self):
        self.assertEqual(discover.classify(b"\x00\x00", -273.14), "absent")

    def test_empty_is_absent(self):
        self.assertEqual(discover.classify(b"", None), "absent")

    def test_plain_zero_stays_answered(self):
        # 0 is a legitimate value (a flag off, a count) — not 'absent' on its own.
        self.assertEqual(discover.classify(b"\x00\x00", 0), "answered")


if __name__ == "__main__":
    unittest.main()
