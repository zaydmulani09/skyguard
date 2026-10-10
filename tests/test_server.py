"""End-to-end in simulation: hub + virtual nodes -> demo -> DRONE -> event row + alerts. Time is sped up 6x."""
import time
from fastapi.testclient import TestClient
from skyguard.core import load_config
from skyguard.server import create_app
from skyguard import hal


def wait(cond, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.5)
    return False


def test_demo_end_to_end(tmp_path):
    cfg = load_config(); cfg["hub"]["db"] = str(tmp_path / "t.db"); cfg["hub"]["snapshots"] = str(tmp_path / "snap")
    app = create_app(cfg)
    with TestClient(app) as c:
        hub = app.state.hub
        assert c.get("/").status_code == 200
        assert c.get("/api/state").json()["fusion"]["level"] == "CLEAR"
        hub.world.speed = 6.0
        assert c.post("/api/demo/start").json()["ok"]
        assert wait(lambda: c.get("/api/state").json()["fusion"]["level"] == "DRONE DETECTED"), "demo never reached DRONE"
        st = c.get("/api/state").json()
        assert st["fusion"]["confidence"] > 0.8 and len(st["fusion"]["sources"]) >= 2
        assert st["lcd"]["l1"].startswith("DRONE") and st["lcd"]["color"] == "flash_red"
        assert st["loc"].get("zone")
        assert wait(lambda: any("Drone detected" in s for s in hal.SimSpeaker.spoken), 10)    # Nest announcement fired
        assert wait(lambda: c.get("/api/state").json()["events"], 5)
        assert wait(lambda: c.get("/api/state").json()["fusion"]["level"] != "DRONE DETECTED", 60), "never cleared after drone left"
        ev = c.get("/api/state").json()["events"][0]
        assert ev["level"] == "DRONE DETECTED" and ev["sim"] == 1 and ev["t_end"]


def test_test_buttons_and_unknown_sensor(tmp_path):
    cfg = load_config(); cfg["hub"]["db"] = str(tmp_path / "t.db"); cfg["hub"]["snapshots"] = str(tmp_path / "snap")
    app = create_app(cfg)
    with TestClient(app) as c:
        assert c.post("/api/test/nonsense").status_code == 400
        assert c.post("/api/test/ble").json()["ok"]
        assert wait(lambda: c.get("/api/state").json()["fusion"]["evidence"]["ble"] > 0.3, 20)     # Remote ID decoded through the full path
        assert c.get("/api/mini").json()["level"] in ("CLEAR", "POSSIBLE", "DRONE DETECTED")


def test_hal_selects_drivers_by_profile():
    cfg = load_config(); w = hal.sim.World(cfg["room"])
    assert isinstance(hal.build(cfg, w, "A")["mic"], hal.sim.SimMic)
    cfg["drivers"] = dict(cfg["profiles"]["unoq"])        # real profile: no webcam/mic on the UNO Q
    out = hal.build(cfg, w, "B")
    assert "camera" not in out and "mic" not in out


def test_remote_node_ingest_http(tmp_path):
    """Node B (UNO Q) posts batches of messages to /api/ingest; hub must register health + detections."""
    cfg = load_config(); cfg["hub"]["db"] = str(tmp_path / "t.db"); cfg["hub"]["snapshots"] = str(tmp_path / "snap")
    app = create_app(cfg)
    with TestClient(app) as c:
        now = time.time()
        batch = [{"type": "health", "node": "Z", "t": now, "cpu": 12.0, "temp": 41.0, "mem": 30.0, "sensors": ["wifi", "ble"], "sim": False},
                 {"type": "rf", "node": "Z", "kind": "wifi", "aps": [{"ssid": "TELLO-AAAAAA", "bssid": "60:60:1F:00:00:01", "rssi": -60, "score": 0.97, "why": "ssid:Tello"}], "sim": False},
                 {"type": "detection", "node": "Z", "sensor": "wifi", "score": 0.97, "meta": {"rssi": -60}, "t": now, "sim": False}]
        assert c.post("/api/ingest", json=batch).json()["ok"]
        st = c.get("/api/state").json()
        assert st["health"]["Z"]["temp"] == 41.0
        assert any(r["node"] == "Z" and r["ssid"] == "TELLO-AAAAAA" for r in st["rf"])
        assert "Z:wifi" in st["latest"]


def test_signed_ingest_rejects_forgery(tmp_path, monkeypatch):
    """Security: with INGEST_KEY set, unsigned / wrongly-signed / replayed batches are refused."""
    import json
    from skyguard.node import sign
    monkeypatch.setenv("INGEST_KEY", "s3cret")
    cfg = load_config(); cfg["hub"]["db"] = str(tmp_path / "t.db"); cfg["hub"]["snapshots"] = str(tmp_path / "snap")
    app = create_app(cfg)
    with TestClient(app) as c:
        body = json.dumps([{"type": "health", "node": "Q", "t": time.time(), "cpu": 1.0, "sensors": [], "sim": False}]).encode()
        ts = str(int(time.time()))
        assert c.post("/api/ingest", content=body).status_code == 401                                          # unsigned
        assert c.post("/api/ingest", content=body, headers={"X-SG-Time": ts, "X-SG-Sig": "00" * 32}).status_code == 401   # forged
        old = str(int(time.time()) - 120)
        assert c.post("/api/ingest", content=body, headers={"X-SG-Time": old, "X-SG-Sig": sign("s3cret", old, body)}).status_code == 401  # replay
        assert c.post("/api/ingest", content=body, headers={"X-SG-Time": ts, "X-SG-Sig": sign("s3cret", ts, body)}).status_code == 200
        assert "Q" in c.get("/api/state").json()["health"]
