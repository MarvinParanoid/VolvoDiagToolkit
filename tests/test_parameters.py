"""Parameter definitions: decoding, validation and the files we ship."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from volvo_diag.volvo import parameters as pdb

REPO = Path(__file__).resolve().parents[1]

SAMPLE = """
vehicle: {platform: P1, engine: D4164T}
ecus:
  ECM: {tx: 0x7E0, rx: 0x7E8}
parameters:
  boost_actual:
    ecu: ECM
    request: "22D123"
    encoding: {type: uint16_be, scale: 0.1}
    unit: kPa
    min: 0
    max: 300
    status: experimental
  egt:
    ecu: ECM
    request: "22D124"
    encoding: {type: uint16_be, scale: 0.1, offset: -40}
    unit: degC
    status: discovered
  regen:
    ecu: ECM
    request: "22D125"
    encoding: {type: bit, bit: 2}
    status: candidate
  mode:
    ecu: ECM
    request: "22D126"
    encoding: {type: enum, length: 1, values: {0: idle, 1: active}}
    status: candidate
  serial:
    ecu: ECM
    request: "22F18C"
    encoding: {type: ascii}
    status: verified
"""


class DefinitionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "test.yaml"
        self.path.write_text(SAMPLE, encoding="utf-8")
        self.db = pdb.load(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_response_prefix_is_derived(self):
        self.assertEqual(self.db["boost_actual"].response_prefix.hex().upper(), "62D123")

    def test_scaled_decode(self):
        self.assertAlmostEqual(self.db["boost_actual"].decode(bytes.fromhex("62D12303F6")), 101.4)

    def test_offset_decode(self):
        self.assertAlmostEqual(self.db["egt"].decode(bytes.fromhex("62D1240BB8")), 260.0)

    def test_bit_and_enum(self):
        self.assertTrue(self.db["regen"].decode(bytes.fromhex("62D12504")))
        self.assertFalse(self.db["regen"].decode(bytes.fromhex("62D12500")))
        self.assertEqual(self.db["mode"].decode(bytes.fromhex("62D12601")), "active")
        self.assertEqual(self.db["mode"].decode(bytes.fromhex("62D126FF")), "unknown(0xFF)")

    def test_ascii(self):
        self.assertEqual(self.db["serial"].decode(b"\x62\xf1\x8cABC123"), "ABC123")

    def test_answer_to_another_identifier_is_refused(self):
        with self.assertRaises(pdb.DecodeError):
            self.db["boost_actual"].decode(bytes.fromhex("62D12403F2"))

    def test_short_answer_is_refused(self):
        with self.assertRaises(pdb.DecodeError):
            self.db["boost_actual"].decode(bytes.fromhex("62D12303"))

    def test_range_check(self):
        parameter = self.db["boost_actual"]
        self.assertTrue(parameter.in_range(101.4))
        self.assertFalse(parameter.in_range(999.0))

    def test_select_filters_by_status(self):
        keys = [p.key for p in self.db.select(min_status="experimental")]
        self.assertIn("boost_actual", keys)
        self.assertIn("serial", keys)
        self.assertNotIn("regen", keys)  # candidate

    def test_select_preserves_the_requested_order(self):
        keys = [p.key for p in self.db.select(["egt", "boost_actual"])]
        self.assertEqual(keys, ["egt", "boost_actual"])

    def test_unknown_status_is_an_error(self):
        bad = Path(self._tmp.name) / "bad.yaml"
        bad.write_text(
            'parameters:\n  x:\n    request: "22D1"\n    status: probably-fine\n', encoding="utf-8"
        )
        with self.assertRaises(pdb.DefinitionError):
            pdb.load(bad)

    def test_unknown_encoding_is_an_error(self):
        bad = Path(self._tmp.name) / "bad2.yaml"
        bad.write_text(
            'parameters:\n  x:\n    request: "22D1"\n    encoding: {type: float128}\n',
            encoding="utf-8",
        )
        with self.assertRaises(pdb.DefinitionError):
            pdb.load(bad)


class ShippedDefinitionsTest(unittest.TestCase):
    def test_volvo_definitions_load(self):
        database = pdb.load(REPO / "definitions" / "volvo")
        self.assertGreater(len(database), 0)
        self.assertIn("ECM", database.ecus)
        self.assertEqual(database.ecus["ECM"].tx_id, 0x7E0)

    def test_no_unverified_volvo_specific_did_claims_a_unit(self):
        """An entry with a unit is a claim about physics; standard PIDs may
        make it, a candidate Volvo DID may not."""
        database = pdb.load(REPO / "definitions" / "volvo")
        for parameter in database:
            if parameter.status == "candidate" and parameter.unit:
                self.fail(f"{parameter.key} is a candidate but already claims {parameter.unit}")

    def test_simulator_definitions_are_not_loaded_by_default(self):
        default = pdb.default_path()
        self.assertTrue(default.exists(), f"{default} is missing")
        self.assertNotIn("simulator", str(default))
        database = pdb.load(default)
        self.assertNotIn("22FE01", [p.request.hex().upper() for p in database])

    def test_simulator_definitions_load(self):
        database = pdb.load(REPO / "definitions" / "simulator")
        self.assertIn("boost_actual", database.parameters)
        self.assertAlmostEqual(
            database["boost_actual"].decode(bytes.fromhex("62FE0103F6")), 101.4
        )


if __name__ == "__main__":
    unittest.main()
