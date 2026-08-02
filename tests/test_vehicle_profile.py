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


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _param(name: str, ident: int) -> str:
    return (f"parameters:\n"
            f"  rpm:\n"
            f"    name: \"{name}\"\n"
            f"    protocol: volvo\n"
            f"    group: 0x11\n"
            f"    identifier: 0x{ident:04X}\n"
            f"    encoding: {{type: uint8}}\n"
            f"    status: candidate\n")


class MultiProfileTest(unittest.TestCase):
    """The point of profiles: loading one car must not pull in another car's
    definitions, even when they share parameter keys like `rpm`."""

    def _tree(self, d: Path) -> None:
        _write(d / "common" / "shared.yaml",
               "parameters:\n"
               "  shared_key:\n"
               "    name: Shared\n"
               "    protocol: volvo\n"
               "    group: 0x11\n"
               "    identifier: 0x0099\n"
               "    encoding: {type: uint8}\n"
               "    status: candidate\n")
        _write(d / "profiles" / "car-a" / "vehicle.yaml", "vehicle:\n  id: car-a\n")
        _write(d / "profiles" / "car-a" / "params.yaml", _param("RPM A", 0x0001))
        _write(d / "profiles" / "car-b" / "vehicle.yaml", "vehicle:\n  id: car-b\n")
        _write(d / "profiles" / "car-b" / "params.yaml", _param("RPM B", 0x0002))

    def test_selecting_a_profile_isolates_its_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._tree(root)
            self.assertEqual(set(pdb.discover_profiles(root)), {"car-a", "car-b"})

            db = pdb.load_profile(root, "car-a")
            self.assertEqual(db.profile_id, "car-a")
            self.assertEqual(db["rpm"].name, "RPM A")     # not "RPM B"
            self.assertIn("shared_key", db.parameters)     # common is always loaded

    def test_ambiguous_selection_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._tree(root)
            with self.assertRaises(pdb.DefinitionError):
                pdb.load_profile(root)                     # two profiles, none chosen

    def test_unknown_profile_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._tree(root)
            with self.assertRaises(pdb.DefinitionError):
                pdb.load_profile(root, "car-z")


if __name__ == "__main__":
    unittest.main()
