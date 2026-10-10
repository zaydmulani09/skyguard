"""A sensor node: runs the drivers it owns + detectors, pushes messages to a sink (local hub callable or HTTP)."""
from __future__ import annotations
import base64, json, logging, os, queue, threading, time
import numpy as np
import cv2
from .core import Detection
from . import hal
from .detectors.acoustic import AcousticDetector
from .detectors.vision import VisionDetector
from .detectors import rf

log = logging.getLogger("skyguard.node")


def health() -> dict:
    h = {"cpu": None, "temp": None, "mem": None}
    try:
        import psutil
        h["cpu"], h["mem"] = psutil.cpu_percent(), psutil.virtual_memory().percent
        t = getattr(psutil, "sensors_temperatures", lambda: {})()
        for k in ("cpu_thermal", "coretemp", "soc_thermal"):
            if t.get(k):
                h["temp"] = t[k][0].current; break
    except Exception:                                        # noqa: BLE001
        pass
    if h["temp"] is None:
        try:
            h["temp"] = int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000
        except Exception:                                    # noqa: BLE001
            pass
    return h


class HttpSink:
    """Remote node -> hub. Bounded queue; drops frames first when the link is slow."""
    def __init__(self, url: str):
        import requests
        self.url, self.q, self.s = url.rstrip("/") + "/api/ingest", queue.Queue(maxsize=200), requests.Session()
        threading.Thread(target=self._run, daemon=True).start()

    def __call__(self, msg: dict):
        try:
            self.q.put_nowait(msg)
        except queue.Full:
            pass

    def _run(self):
        while True:
            batch = [self.q.get()]
            while not self.q.empty() and len(batch) < 40:
                batch.append(self.q.get_nowait())
            try:
                self.s.post(self.url, json=batch, timeout=4)
            except Exception as e:                           # noqa: BLE001
                log.debug("hub unreachable: %s", e); time.sleep(1)


class NodeRunner:
    def __init__(self, node_id: str, cfg: dict, world, sink, roles: set[str] | None = None, is_sim: bool | None = None):
        self.id, self.cfg, self.sink = node_id, cfg, sink
        self.sim = (cfg["drivers"].get("camera") == "sim") if is_sim is None else is_sim
        self.dr = hal.build(cfg, world, node_id, roles)
        self.stop = threading.Event()
        self.ac = AcousticDetector(os.path.join(os.path.dirname(os.path.dirname(__file__)), cfg["acoustic"]["model"]))
        self.vis = None
        if "camera" in self.dr and self.dr["camera"] is not None:
            kind = self.dr.get("vision_kind", "sim")
            self.vis = VisionDetector(cfg, kind, hal.sim.SimVision(self.dr["camera"], cfg) if kind == "sim" else None)
        self.track = rf.RssiTracker()
        self.threads: list[threading.Thread] = []

    def emit(self, sensor: str, score: float, meta: dict | None = None):
        self.sink({"type": "detection", **Detection(self.id, sensor, score, meta or {}, sim=self.sim).to_dict()})

    # --- loops ---
    def _acoustic(self):
        mic = self.dr["mic"]
        while not self.stop.is_set():
            try:
                x = mic.read(1.0)
                s, meta = self.ac.detect(x)
                meta["rms_db"] = round(20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9), 1)
                self.emit("acoustic", s, meta)
                self.sink({"type": "spec", "node": self.id, "rows": self.ac.spectrogram(x), "score": round(s, 3)})
            except Exception as e:                           # noqa: BLE001
                log.warning("acoustic: %s", e); time.sleep(1)

    def _vision(self):
        cam, last_det, last_fr = self.dr["camera"], 0, 0
        while not self.stop.is_set():
            fr = cam.read()
            if fr is None:
                time.sleep(0.2); continue
            try:
                s, meta, boxes = self.vis.process(fr)
            except Exception as e:                           # noqa: BLE001
                log.warning("vision: %s", e); time.sleep(0.5); continue
            now = time.time()
            if now - last_det > 0.5:
                self.emit("vision", s, meta); last_det = now
            if now - last_fr > 0.25:
                last_fr = now
                small = cv2.resize(fr, (480, int(fr.shape[0] * 480 / fr.shape[1])))
                k = 480 / fr.shape[1]
                for b in boxes:
                    p1, p2 = (int(b["x"] * k), int(b["y"] * k)), (int((b["x"] + b["w"]) * k), int((b["y"] + b["h"]) * k))
                    col = (60, 60, 255) if b["conf"] > 0.5 else (0, 200, 255)
                    cv2.rectangle(small, p1, p2, col, 2)
                    cv2.putText(small, f"drone {b['conf']:.2f}", (p1[0], max(12, p1[1] - 5)), cv2.FONT_HERSHEY_SIMPLEX, .5, col, 1, cv2.LINE_AA)
                ok, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 62])
                if ok:
                    self.sink({"type": "frame", "node": self.id, "jpg": base64.b64encode(jpg).decode(), "n": len(boxes), "score": round(s, 3)})
            time.sleep(0.12 if self.sim else 0.02)

    def _wifi(self):
        sc, every = self.dr["wifi"], self.cfg["rf"]["wifi_scan_s"] if not self.sim else 1.5
        while not self.stop.is_set():
            t0 = time.time()
            try:
                aps = sc.scan()
            except Exception as e:                           # noqa: BLE001
                log.warning("wifi scan: %s", e); aps = []
            rows, best = [], None
            for a in aps:
                sc_, why = rf.classify_ap(a["ssid"], a["bssid"])
                self.track.add(a["bssid"], a["rssi"])
                st = self.track.state(a["bssid"])
                row = {**a, "score": sc_, "why": why, "trend": st["trend"], "slope": st["slope"], "rssi_s": st["rssi"]}
                rows.append(row)
                if sc_ >= 0.5 and (best is None or sc_ > best["score"]):
                    best = row
            self.sink({"type": "rf", "node": self.id, "kind": "wifi", "aps": sorted(rows, key=lambda r: -r["rssi"])[:25], "sim": self.sim})
            if best:
                self.emit("wifi", best["score"], {"ssid": best["ssid"], "bssid": best["bssid"], "rssi": best["rssi_s"],
                                                   "trend": best["trend"], "why": best["why"]})
            else:
                self.emit("wifi", 0.0, {})
            self.stop.wait(max(0.2, every - (time.time() - t0)))

    def _ble(self):
        sc, every = self.dr["ble"], self.cfg["rf"]["ble_scan_s"] if not self.sim else 2.0
        while not self.stop.is_set():
            t0 = time.time()
            try:
                devs = sc.scan()
            except Exception as e:                           # noqa: BLE001
                log.warning("ble scan: %s", e); devs = []
            rid, rows = None, []
            for d in devs:
                p = d.get("service_data", {}).get(rf.RID_UUID16)
                dec = rf.decode_remote_id(p) if p else None
                rows.append({"addr": d["addr"], "name": d["name"] or ("Remote ID" if dec else ""), "rssi": d["rssi"], "remote_id": dec is not None})
                if dec:
                    rid = {**dec, "rssi": d["rssi"], "addr": d["addr"]}
            self.sink({"type": "rf", "node": self.id, "kind": "ble", "aps": rows, "sim": self.sim})
            self.emit("ble", 0.95 if rid else 0.0, {"remote_id": True, **rid} if rid else {})
            self.stop.wait(max(0.2, every - (time.time() - t0)))

    def _health(self):
        while not self.stop.is_set():
            self.sink({"type": "health", "node": self.id, "t": time.time(), **health(), "sim": self.sim,
                       "sensors": sorted(k for k in self.dr if k in ("camera", "mic", "wifi", "ble") and self.dr[k] is not None)})
            self.stop.wait(2.0)

    def start(self):
        spec = [("mic", self._acoustic), ("camera", self._vision), ("wifi", self._wifi), ("ble", self._ble)]
        for k, fn in spec:
            if self.dr.get(k) is not None and (k != "camera" or self.vis):
                self.threads.append(threading.Thread(target=fn, name=f"{self.id}-{k}", daemon=True))
        self.threads.append(threading.Thread(target=self._health, daemon=True))
        for t in self.threads:
            t.start()
        log.info("node %s up: %s", self.id, sorted(k for k in self.dr if self.dr[k] is not None))
        return self

    def close(self):
        self.stop.set()
