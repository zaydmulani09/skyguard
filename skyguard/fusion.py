"""Weighted, time-windowed, multi-node evidence fusion -> confidence + threat level.

For sensor s on node n the evidence is the decayed max score inside the window:
    e[s,n] = max_i  score_i * exp(-(now - t_i) / tau),   tau = window/2,  dropped when older than window
Persistence: a sensor counts fully only if it fired (>=0.5) at least twice within PERSIST_S; a lone blip is scaled by
LONE=0.45 so independent one-off false positives cannot line up across sensors (Remote ID decodes are exempt).
Per sensor we take the best node, E_s = max_n e[s,n]. Then (logistic, i.e. weighted log-odds):
    z = b + sum_s w_s * E_s  + a_sensor * (K-1) + a_node * (#sensors seen by both nodes)
    confidence = sigmoid(z),  K = #sensors with E_s > 0.5
Corroboration terms are why one noisy sensor cannot reach DRONE alone: a lone sensor tops out ~45-60%.
Hysteresis: enter DRONE at >= enter, leave when < exit and held for min_hold_s. Single-sensor lock-out: DRONE
needs K>=2, or a decoded Remote ID (legally mandated broadcast, ~definitive).
"""
from __future__ import annotations
import math, time
from .core import Detection, SENSORS

CLEAR, POSSIBLE, DRONE = "CLEAR", "POSSIBLE", "DRONE DETECTED"
W = {"vision": 4.2, "acoustic": 3.2, "wifi": 3.6, "ble": 6.5}
BIAS, A_SENSOR, A_NODE = -4.2, 1.6, 0.8
PERSIST_S = {"vision": 4.0, "acoustic": 4.0, "wifi": 8.0}   # a blip must repeat within this span to count fully
LONE = 0.45                                                 # evidence multiplier for a one-off positive (ble Remote ID exempt)


class Fusion:
    def __init__(self, cfg: dict | None = None):
        c = {"window_s": 8.0, "enter": 0.80, "exit": 0.60, "possible": 0.35, "min_hold_s": 5.0}
        c.update(cfg or {})
        self.c = c
        self.events: list[Detection] = []
        self.level = CLEAR
        self._since = 0.0
        self.conf = 0.0
        self.scale: dict[str, float] = {}      # context weights, e.g. vision x0.6 in the dark

    def add(self, d: Detection):
        self.events.append(d)

    def _evidence(self, now: float):
        w = self.c["window_s"]; tau = w / 2
        self.events = [d for d in self.events if now - d.t <= w]
        ev: dict[str, dict[str, float]] = {s: {} for s in SENSORS}
        hits: dict[tuple, int] = {}
        for d in self.events:
            if d.score >= 0.5 and now - d.t <= PERSIST_S.get(d.sensor, 0):
                hits[(d.node, d.sensor)] = hits.get((d.node, d.sensor), 0) + 1
        for d in self.events:
            persist = 1.0 if d.sensor == "ble" or hits.get((d.node, d.sensor), 0) >= 2 else LONE
            e = d.score * math.exp(-(now - d.t) / tau) * persist
            if e > ev[d.sensor].get(d.node, 0):
                ev[d.sensor][d.node] = e
        return ev

    def step(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        ev = self._evidence(now)
        E = {s: (max(ev[s].values()) if ev[s] else 0.0) for s in SENSORS}
        active = [s for s in SENSORS if E[s] > 0.5]
        both = [s for s in SENSORS if len(ev[s]) >= 2 and min(ev[s].values()) > 0.4]
        z = BIAS + sum(W[s] * self.scale.get(s, 1.0) * E[s] for s in SENSORS) + A_SENSOR * max(0, len(active) - 1) + A_NODE * len(both)
        conf = 1 / (1 + math.exp(-z))
        rid = any(d.sensor == "ble" and d.meta.get("remote_id") and now - d.t < self.c["window_s"] for d in self.events)
        gate = len(active) >= 2 or rid
        c = self.c
        prev = self.level
        if self.level == DRONE:
            if conf < c["exit"] and now - self._since > c["min_hold_s"]:
                self.level = POSSIBLE if conf >= c["possible"] else CLEAR
        elif conf >= c["enter"] and gate:
            self.level = DRONE
        elif conf >= c["possible"]:
            self.level = POSSIBLE
        else:
            self.level = CLEAR
        if self.level == DRONE and prev != DRONE:
            self._since = now
        self.conf = conf
        return {"t": now, "confidence": round(conf, 4), "level": self.level, "changed": prev != self.level,
                "evidence": {s: round(E[s], 3) for s in SENSORS}, "sources": active, "nodes_by_sensor": {s: sorted(ev[s]) for s in SENSORS if ev[s]},
                "cross_node": both, "z": round(z, 2)}


SRC = {"vision": "VIS", "acoustic": "AUD", "wifi": "RF", "ble": "RID"}


def source_tag(sources: list[str]) -> str:
    return "+".join(dict.fromkeys(SRC[s] for s in sources)) or "-"
