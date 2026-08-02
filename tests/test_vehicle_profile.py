"""The vehicle profile (buses + configuration) is data, not code: adding a car is
a YAML exercise. These lock in that the bundled P1 profile parses, that the
J2534 open parameters survive the round trip, and that a definition set with no
profile still falls back to a working default."""

import tempfile
import unittest
from pathlib import Path

from volvo_diag.volvo import parameters as pdb


class BundledProfileTest(unittest.TestCase):
    def setUp(self):
        self.db = pdb.load(pdb.default_path())

    def test_buses_parse_with_j2534_open_params(self):
        hs, ls = self.db.bus("hs"), self.db.bus("ls")
        self.assertEqual((hs.baudrate, hs.protocol, hs.obd), (500000, 5, True))
        self.assertEqual((ls.baudrate, ls.protocol, ls.obd), (125000, 32772, False))
        # the vendor selector and sample point the low-speed connect needs
        self.assertEqual(ls.vendor_params, {0x8001: 779})
        self.assertEqual(ls.sample_point, 68)

    def test_module_map_and_helpers(self):
        self.assertEqual(self.db.primary_bus().id, "hs")
        self.assertEqual(self.db.bus_for_module("DIM"), "ls")   # cabin-only -> low-speed
        self.assertEqual(self.db.bus_for_module("ECM"), "hs")
        # ELM reaches OBD-accessible buses only
        self.assertEqual([b.id for b in self.db.serve_buses() if b.obd], ["hs"])

    def test_configuration_topology(self):
        c = self.db.config_topology()
        self.assertEqual((c.ecu, c.bus), ("CEM", "hs"))
        self.assertEqual((c.identity_block, c.config_block), (0xFB, 0xFC))


class FallbackAndInferenceTest(unittest.TestCase):
    def test_no_profile_falls_back_to_default(self):
        empty = pdb.Database()
        self.assertEqual([b.id for b in empty.serve_buses()],
                         [b.id for b in pdb.DEFAULT_BUSES])
        self.assertEqual(empty.config_topology().ecu, "CEM")

    def test_obd_flag_is_inferred_when_omitted(self):
        yaml_text = (
            "buses:\n"
            "  - id: a\n"
            "    baudrate: 500000\n"
            "    j2534_protocol: 5\n"
            "    modules: [ECM]\n"
            "  - id: b\n"
            "    baudrate: 125000\n"
            "    j2534_protocol: 32772\n"
            "    vendor_params: {0x8001: 779}\n"
            "    modules: [DIM]\n"
        )
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "prof.yaml"
            path.write_text(yaml_text, encoding="utf-8")
            db = pdb.load_file(path)
        # plain 500k CAN -> OBD-reachable; a vendor low-speed bus -> not
        self.assertTrue(db.bus("a").obd)
        self.assertFalse(db.bus("b").obd)
        self.assertEqual(db.bus("b").vendor_params, {0x8001: 779})


if __name__ == "__main__":
    unittest.main()
