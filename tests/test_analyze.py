"""Post-drive trip analysis (volvo_diag.analyze)."""

import csv
import math
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from volvo_diag import analyze


def _synthetic_trip(path: Path) -> None:
    header = ["t", "rpm", "boost_actual", "boost_requested", "coolant_temperature",
              "exhaust_temperature", "maf", "regeneration_active"]
    rows = [header]
    for i in range(200):
        t = i * 0.5
        coolant = min(90.0, 20 + t * 0.9)                 # warms up over the run
        ba = 140 + 20 * math.sin(t / 4)
        br = ba + (8 if 20 < t < 25 else 2)               # a brief tracking error
        egt = 200 + (360 if 40 <= t <= 80 else 0)         # a regen window
        rows.append([f"{t:.2f}", round(850 + 200 * math.sin(t / 5), 1), round(ba, 1),
                     round(br, 1), round(coolant, 1), round(egt, 1),
                     round(30 + 8 * math.sin(t / 3), 1), 1 if 42 <= t <= 78 else 0])
    with path.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)


class AnalyzeTest(unittest.TestCase):
    def setUp(self):
        self._dir = TemporaryDirectory()
        self.csv = Path(self._dir.name) / "trip.csv"
        _synthetic_trip(self.csv)
        self.times, self.cols = analyze.load_trip(self.csv)
        self.roles = analyze.detect_roles(self.cols)
        self.report = analyze.analyze(self.times, self.cols, self.roles)

    def tearDown(self):
        self._dir.cleanup()

    def test_roles_detected(self):
        for role in ("rpm", "boost", "boost_req", "egt", "coolant", "regen"):
            self.assertIn(role, self.roles)

    def test_warmup_detected(self):
        self.assertIsNotNone(self.report["warmup"]["to_70C_s"])
        self.assertIsNotNone(self.report["warmup"]["to_88C_s"])

    def test_boost_deviation(self):
        self.assertAlmostEqual(self.report["boost"]["max_dev"], 8.0, places=1)

    def test_regeneration_window_found(self):
        self.assertTrue(self.report["regen"])
        self.assertGreater(self.report["regen"][0]["duration_s"], 20)

    def test_reports_render(self):
        text = analyze.format_text(self.report, self.roles)
        self.assertIn("probable regeneration", text)
        html = analyze.format_html(self.times, self.cols, self.roles, self.report)
        self.assertIn("<canvas>", html)
        self.assertIn("Trip report", html)


if __name__ == "__main__":
    unittest.main()
