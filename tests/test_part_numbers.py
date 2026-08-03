"""Part-number decode from the F5 identity block (BCD number + 2-letter revision),
against real dumps read off the car."""

import unittest

from volvo_diag.volvo import config


class PartNumberTest(unittest.TestCase):
    def test_bpm_part_numbers(self):
        # dump-bpm F5, read off the installed (old) BPM
        raw = bytes.fromhex("ffffffffffffffffffffffff003128238020414"
                            "2ffc000000031282516204141fffa0000000000")
        self.assertEqual(config.decode_part_numbers(raw),
                         [("31282380", "AB"), ("31282516", "AA")])

    def test_cem_part_numbers(self):
        raw = bytes.fromhex("003129661020414100004400003134315820414100"
                            "004000003129611020414100fe8000000000")
        self.assertEqual([n for n, _ in config.decode_part_numbers(raw)],
                         ["31296610", "31343158", "31296110"])

    def test_empty(self):
        self.assertEqual(config.decode_part_numbers(b""), [])


if __name__ == "__main__":
    unittest.main()
