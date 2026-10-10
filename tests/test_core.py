import math, time, numpy as np
from skyguard.core import ROOT, load_config, Detection, zone_of, compass, bearing_deg
from skyguard.fusion import Fusion, CLEAR, POSSIBLE, DRONE
from skyguard.localize import Localizer
from skyguard.detectors import rf
from skyguard.detectors.acoustic import AcousticDetector
from skyguard.hal.sim import synth_drone, synth_noise, synth_speech

cfg = load_config()


def D(sensor, score, node="A", t=0.0, **meta):
    return Detection(node, sensor, score, meta, t)


def add2(f, sensor, score, node="A", t=0.0):
    """A sensor firing on two consecutive ticks (persistent evidence)."""
    f.add(D(sensor, score, node, t - 1.0)); f.add(D(sensor, score, node, t))


# ---------------- fusion ----------------
def test_single_sensor_never_drone():
    f = Fusion(cfg["fusion"])
    for s in ("vision", "acoustic", "wifi"):
        f2 = Fusion(cfg["fusion"]); f2.add(D(s, 1.0, t=100))
        r = f2.step(100)
        assert r["level"] != DRONE and r["confidence"] < 0.8, (s, r)


def test_two_sensors_trigger_and_clear():
    f = Fusion(cfg["fusion"])
    add2(f, "acoustic", 0.8, t=100); add2(f, "wifi", 0.9, t=100)
    r = f.step(100)
    assert r["level"] == DRONE and r["confidence"] > 0.9 and set(r["sources"]) == {"acoustic", "wifi"}
    assert f.step(130)["level"] == CLEAR                      # evidence aged out of the window


def test_cross_node_adds_evidence():
    a = Fusion(cfg["fusion"]); add2(a, "acoustic", .7, "A", 10); add2(a, "wifi", .7, "A", 10)
    b = Fusion(cfg["fusion"]); [add2(b, s_, .7, n_, 10) for s_ in ("acoustic", "wifi") for n_ in "AB"]
    assert b.step(10)["confidence"] > a.step(10)["confidence"]
    assert b.step(10)["cross_node"] == ["acoustic", "wifi"]


def test_remote_id_alone_is_enough():
    f = Fusion(cfg["fusion"]); f.add(D("ble", 0.95, t=5, remote_id=True))
    assert f.step(5)["level"] == DRONE


def test_hysteresis_holds():
    f = Fusion(cfg["fusion"]); add2(f, "acoustic", .9, t=0); add2(f, "wifi", .9, t=0)
    assert f.step(0)["level"] == DRONE
    assert f.step(3)["level"] == DRONE                        # still in hold window


def test_lone_blips_do_not_align():
    """One-off false positives on two sensors within the window must not make a DRONE."""
    f = Fusion(cfg["fusion"])
    f.add(D("acoustic", 0.95, t=100)); f.add(D("vision", 0.95, t=101)); f.add(D("wifi", 0.95, t=102))
    r = f.step(102)
    assert r["level"] != DRONE and r["sources"] == [], r


def test_noise_stays_clear():
    f = Fusion(cfg["fusion"]); rng = np.random.default_rng(0)
    for i in range(200):
        s = ("acoustic", "vision", "wifi")[i % 3]
        f.add(D(s, float(rng.uniform(0, .3)), t=i * .5))
        assert f.step(i * .5)["level"] == CLEAR


# ---------------- localization ----------------
def test_localizer_finds_zone_and_trend():
    L = Localizer(cfg["room"], cfg["rf"])
    true = (9.0, 6.0)                                         # NE zone
    rng = np.random.default_rng(1)
    for i in range(30):
        r = {n: float(L.rssi_at(n, *true) + rng.normal(0, 2)) for n in "AB"}
        out = L.update(r, t=i * .5)
    assert out["zone"] in ("NE", "N", "E"), out
    assert math.hypot(out["x"] - true[0], out["y"] - true[1]) < 3.0, out


def test_trend_approaching_leaving():
    L = Localizer(cfg["room"], cfg["rf"])
    for i in range(20):
        out = L.update({"A": -80 + i * 1.0, "B": -75 + i * 0.8}, t=i * .5 * 2)
    assert out["trend"] == "approaching"
    L.reset()
    for i in range(20):
        out = L.update({"A": -60 - i * 1.0, "B": -60 - i * .8}, t=i * 1.0)
    assert out["trend"] == "leaving"


def test_geometry():
    assert zone_of(1, 7, 12, 8) == "NW" and zone_of(11, 1, 12, 8) == "SE" and zone_of(6, 4, 12, 8) == "C"
    assert compass(bearing_deg((0, 0), (5, 5))) == "NE" and compass(bearing_deg((0, 0), (-5, 0))) == "W"


# ---------------- RF matcher ----------------
def test_ssid_patterns():
    for ssid in ("TELLO-5A3F2C", "DJI-MAVIC-AIR2", "WIFI_UFO_8812", "FPV-A1B2", "my_drone_ap", "ANAFI-0123"):
        assert rf.classify_ap(ssid)[0] >= 0.5, ssid
    for ssid in ("xfinitywifi", "PHS Hack-A-Thon", "Home-5G", "DIRECT-roku-123", "NETGEAR42", "Starbucks", "Droneshop-guest-hmm" if False else "eduroam"):
        assert rf.classify_ap(ssid)[0] < 0.5, ssid
    assert 0.2 < rf.classify_ap("ESP_3D1A7C")[0] < 0.5       # weak lookalike never fires alone


def test_oui_and_randomised_mac():
    assert rf.classify_ap("", "60:60:1F:11:22:33")[0] >= 0.5
    assert rf.classify_ap("", "62:60:1F:11:22:33")[0] == 0        # locally-administered bit set -> ignore
    s_both = rf.classify_ap("TELLO-AABBCC", "60:60:1F:11:22:33")[0]
    assert s_both > rf.classify_ap("TELLO-AABBCC")[0]


def test_rssi_tracker_trend():
    t = rf.RssiTracker(); now = time.time()
    for i in range(10):
        t.add("x", -80 + 2 * i, now + i)
    assert t.state("x")["trend"] == "approaching"
    for i in range(10):
        t.add("y", -50 - 2 * i, now + i)
    assert t.state("y")["trend"] == "leaving"


def test_remote_id_roundtrip():
    p = rf.encode_remote_id_for_tests("1581F4XYZ0A1234", 33.77, -84.38, 38.0, 3.5, 200)
    d = rf.decode_remote_id(p)
    assert d["id"] == "1581F4XYZ0A1234" and abs(d["lat"] - 33.77) < 1e-5 and abs(d["lon"] + 84.38) < 1e-5
    assert abs(d["height_agl_m"] - 38) < 1 and d["ua_type"].startswith("Helicopter") and d["direction_deg"] == 200
    assert rf.decode_remote_id(b"\x00\x01") is None


# ---------------- acoustic ----------------
def test_acoustic_separates_synthetic_classes():
    det = AcousticDetector(ROOT / "models" / "acoustic.json")        # model tuned on the real dataset
    rng = np.random.default_rng(0)
    drone = [det.detect(0.3 * synth_noise(1, rng) + 0.3 * synth_drone(1, snr, rng, f))[0] for f in (150, 185, 230) for snr in (-2, -6)]
    other = ([det.detect(0.3 * synth_noise(1, rng))[0] for _ in range(4)] + [det.detect(0.3 * synth_noise(1, rng, kind="white"))[0] for _ in range(3)]
             + [det.detect(0.3 * synth_noise(1, rng) + 0.25 * synth_speech(1, rng))[0] for _ in range(4)])
    assert np.mean(np.array(drone) > 0.5) >= 0.8, drone                 # recall on synthetic rotors
    assert np.mean(np.array(other) > 0.5) <= 0.1, other                 # speech / fan / white noise rejected


def test_acoustic_features_comb_detects_harmonics():
    from skyguard.detectors.acoustic import features
    rng = np.random.default_rng(2)
    f = features(synth_drone(1, 10, rng, 200) + 0.2 * synth_noise(1, rng))
    g = features(synth_noise(1, rng))
    assert f["comb"] > g["comb"] + 3 and abs(f["f0"] - 200) < 12
