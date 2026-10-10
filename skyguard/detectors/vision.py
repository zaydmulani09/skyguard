"""Vision: YOLOv8-nano drone detector + centroid tracker + bearing/motion estimate.

Weights priority (first that exists): ONNX (fast on Pi 4 CPU) -> fine-tuned .pt -> stock COCO yolov8n with a
sky-object proxy (airplane/bird/kite, capped score 0.4) so the pipeline always runs.  Which one is live is
reported in meta['model'] and shown on the dashboard.
Bearing: theta = heading + (cx/W - 0.5) * HFOV   (pinhole, small-angle).  Motion: d(area)/dt > 0 => approaching.
"""
from __future__ import annotations
import logging, math
from pathlib import Path
import numpy as np
from ..core import ROOT, compass

log = logging.getLogger("skyguard.vision")
COCO_SKY = {4: "airplane", 14: "bird", 38: "kite"}


class Tracker:
    """Greedy nearest-centroid tracker. A drone must persist >= min_hits frames before it counts (kills 1-frame FPs)."""
    def __init__(self, max_dist=90, max_age=8, min_hits=3):
        self.tracks: dict[int, dict] = {}; self.nid = 0
        self.max_dist, self.max_age, self.min_hits = max_dist, max_age, min_hits

    def update(self, boxes: list[dict]) -> list[dict]:
        for t in self.tracks.values():
            t["age"] += 1
        for b in sorted(boxes, key=lambda b: -b["conf"]):
            cx, cy = b["x"] + b["w"] / 2, b["y"] + b["h"] / 2
            best, bd = None, self.max_dist
            for tid, t in self.tracks.items():
                d = math.hypot(cx - t["cx"], cy - t["cy"])
                if d < bd and t["age"] > 0:
                    best, bd = tid, d
            if best is None:
                self.nid += 1; best = self.nid
                self.tracks[best] = {"hits": 0, "area0": b["w"] * b["h"], "hist": []}
            t = self.tracks[best]
            t.update(cx=cx, cy=cy, age=0, box=b, conf=b["conf"]); t["hits"] += 1
            t["hist"] = (t["hist"] + [(cx, cy, b["w"] * b["h"])])[-12:]
        self.tracks = {k: t for k, t in self.tracks.items() if t["age"] <= self.max_age}
        return [dict(id=k, **t) for k, t in self.tracks.items() if t["age"] == 0 and t["hits"] >= self.min_hits]


class VisionDetector:
    def __init__(self, cfg: dict, kind: str = "yolo", sim_det=None, cam_w: int = 640):
        self.cfg, self.kind, self.sim_det = cfg, kind, sim_det
        self.tr = Tracker()
        self.heading, self.hfov = cfg["room"]["camera_heading_deg"], cfg["room"]["camera_hfov_deg"]
        self.model, self.model_name, self.names = None, "sim" if kind == "sim" else "none", {}
        if kind == "yolo":
            self._load()

    def _load(self):
        v = self.cfg["vision"]
        for p, tag in ((ROOT / v["onnx"], "drone-onnx"), (ROOT / v["weights"], "drone-pt")):
            if p.exists():
                try:
                    from ultralytics import YOLO
                    self.model, self.model_name = YOLO(str(p), task="detect"), tag
                    log.info("vision model: %s", p); return
                except Exception as e:                      # noqa: BLE001
                    log.warning("load %s failed: %s", p, e)
        try:
            from ultralytics import YOLO
            self.model, self.model_name = YOLO("yolov8n.pt"), "coco-proxy"
            log.warning("no drone weights -> COCO proxy (airplane/bird/kite)")
        except Exception as e:                              # noqa: BLE001
            log.warning("no vision model at all: %s", e)

    def _infer(self, frame) -> list[dict]:
        if self.kind == "sim":
            return self.sim_det.detect(frame)
        if self.model is None:
            return []
        r = self.model.predict(frame, imgsz=self.cfg["vision"]["imgsz"], conf=self.cfg["vision"]["conf"], verbose=False)[0]
        out = []
        for b in r.boxes:
            c = int(b.cls[0]); conf = float(b.conf[0])
            if self.model_name == "coco-proxy":
                if c not in COCO_SKY:
                    continue
                conf = min(conf, 0.4)
            x1, y1, x2, y2 = (float(v) for v in b.xyxy[0])
            out.append({"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1, "conf": conf})
        return out

    def process(self, frame) -> tuple[float, dict, list[dict]]:
        """-> (score, meta, boxes-for-overlay). Score = best confirmed-track confidence; unconfirmed boxes shown but score<=0.25."""
        boxes = self._infer(frame)
        confirmed = self.tr.update(boxes)
        H, W = frame.shape[:2]
        meta = {"model": self.model_name, "n": len(boxes)}
        if confirmed:
            t = max(confirmed, key=lambda t: t["conf"])
            bearing = (self.heading + (t["cx"] / W - 0.5) * self.hfov) % 360
            h = t["hist"]
            growth = (h[-1][2] - h[0][2]) / max(h[0][2], 1) if len(h) > 3 else 0.0
            dx = (h[-1][0] - h[0][0]) / W if len(h) > 3 else 0.0
            meta.update(bearing=round(bearing, 1), compass=compass(bearing), track=t["id"], hits=t["hits"],
                        motion="approaching" if growth > 0.08 else "receding" if growth < -0.08 else "hovering/lateral",
                        lateral="right" if dx > 0.02 else "left" if dx < -0.02 else "centre", box_frac=round(t["box"]["w"] * t["box"]["h"] / (W * H), 4))
            return float(min(1.0, t["conf"] * (0.7 + 0.1 * min(t["hits"], 3)))), meta, boxes
        return float(min(0.25, max((b["conf"] for b in boxes), default=0.0) * 0.5)), meta, boxes
