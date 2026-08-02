"""Offline checks for the configuration decode.

The frames below are the real 0xFB identity response the CEM gave in a captured
VIDA session (logs/j2534-20260801-170028-...-identification.jsonl), each the
8 data bytes of one CAN frame. Decoding them must reproduce the car's known VIN
and market — this is what pins the fixed-offset scheme the car-config decode
relies on. Run with pytest, or directly: `python python/tests/test_config.py`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from volvo_diag.transport.volvo_ecm import ReplayLink, VolvoEcm  # noqa: E402
from volvo_diag.volvo import config as configmod  # noqa: E402

# One CEM 0xFB block, as captured (first frame 0x9x, then 0x1x consecutive).
FB_FRAMES = [
    "9750f9fbfe003182", "1197783030310d0a", "125956314d573736", "1335323932343833",
    "143031350d0a3238", "1535313535303733", "160d0a3534350d0a", "173438333031350d",
    "100a32320d0a3230", "11303835300d0a30", "1230303030343137", "1331313335363933",
    "1431373336320d0a", "1530303030303030", "1630303030303030", "1730303030303030",
    "1030303030303030", "1130303030303030", "123030300d0a3138", "130d0a354337370d",
    "140a34353531360d", "150a45553030380d", "160a313332323531", "173333364436310d",
    "100a303030303030", "1130303030303030", "1230303030303030", "1330303030303030",
    "1430303030303030", "1530303030303030", "1630303030303030", "1730303030303030",
    "1030303030303030", "1130303030303030", "1230303030303030",
]


def _identity():
    frames = [bytes.fromhex(h) for h in FB_FRAMES]
    link = ReplayLink({}, response_can_id=0x400003, identity_frames=frames)
    link.open()
    ecm = VolvoEcm(link, group=0x50, timeout=0.5)
    raw = ecm.read_block(0xFB, group=0x50)
    return {f.name: f.value for f in configmod.decode_identity(raw, configmod.load_map())}


def test_identity_vin():
    assert _identity()["VIN"] == "YV1MW765292483015"


def test_identity_market_and_chassis():
    fields = _identity()
    assert fields["Market code"] == "EU008"
    assert fields["Chassis"] == "483015"
    assert fields["Structure week"] == "200850"


def test_car_config_decodes_known_bytes():
    # A hand-built 0xFC block: byte 2 = Vehicle sub type 0x03 (V50),
    # byte 4 = Gearbox 0x07 (M66). Confirms the byte-offset decode + labels.
    raw = bytes([0x00, 0x00, 0x03, 0x00, 0x07] + [0x00] * 200)
    options = {o.name: o.label for o in configmod.decode_car_config(raw, configmod.load_map())}
    assert options["Vehicle sub type"] == "V50"
    assert options["Gearbox"] == "M66"


# The CEM's real 0xFC car-configuration block, reassembled from a captured VIDA
# session (logs/...-21-car-config.jsonl) — first data byte is the checksum, the
# options follow at their catalogued offsets. Decoding it must reproduce this
# car's actual factory coding; this pins the decode against real data, not a
# hand-built sample.
FC_BLOCK = bytes.fromhex(
    "8c3f030209010304020105010301020103010101010202293302010201010101"
    "0201030203010205050102010102000201010102011202030102000001010101"
    "01010101010105010101010105020303010101020109010201010101010202010"
    "2020202010101010102020107080102030201010000000000000000000000000000"
    "00000000000000"
)


def test_car_config_decodes_captured_block():
    opts = {o.name: o.label for o in configmod.decode_car_config(FC_BLOCK, configmod.load_map())}
    assert opts["Vehicle sub type"] == "V50"
    assert opts["Doors"] == "5 doors"
    assert opts["Gearbox"] == "MTX75"
    assert opts["Gearbox type"] == "Manual gearbox"
    assert opts["Fuel"] == "Diesel"
    assert opts["Engine"] == "D4164T"
    assert opts["Cruise control"] == "Yes"
    assert opts["Particle Filter For Diesel"].startswith("Yes")


if __name__ == "__main__":
    test_identity_vin()
    test_identity_market_and_chassis()
    test_car_config_decodes_known_bytes()
    test_car_config_decodes_captured_block()
    print("ok: identity + car-config decode (incl. captured 0xFC block)")
