"""How much does fusion cut false alarms? Replays 1 h of 'quiet venue' (1 s ticks) and 1000 drone events through
  (a) each sensor alone with a plain threshold, (b) the SkyGuard fusion.
Audio scores are the REAL held-out predictions from scripts/eval_audio.py (results/audio_test_scores.npz). Other sensors use stated
per-tick rates (see ASSUMED) because we have no labelled field data for them -- change them and re-run.
Usage: python scripts/eval_fusion.py -> results/fusion_eval.json"""
import json, sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from skyguard.core import load_config, Detection
from skyguard.fusion import Fusion, DRONE

ROOT = Path(__file__).resolve().parent.parent
cfg = load_config()
z = np.load(ROOT / "results" / "audio_test_scores.npz")
cal = lambda p: 1 / (1 + np.exp(-10 * (p - float(z["thr"]))))      # same calibration as AcousticDetector.calibrated
AUD_NEG, AUD_POS = cal(z["p"][z["y"] == 0]), cal(z["p"][z["y"] == 1])
ASSUMED = {"vision_fp_per_tick": 0.01, "vision_tp_per_tick": 0.80, "wifi_fp_per_tick": 0.003, "wifi_tp_per_tick": 0.90, "ble_fp_per_tick": 0.0005}
rng = np.random.default_rng(0)


def tick(drone: bool, t: float, f: Fusion):
    """Feed one second of detector outputs (node A: all sensors, node B: wifi+ble)."""
    A = ASSUMED
    ev = []
    ev.append(("A", "acoustic", float(rng.choice(AUD_POS if drone else AUD_NEG))))
    ev.append(("A", "vision", float(rng.uniform(.5, .95)) if rng.random() < (A["vision_tp_per_tick"] if drone else A["vision_fp_per_tick"]) else 0.0))
    for nd in "AB":
        ev.append((nd, "wifi", float(rng.uniform(.6, .97)) if rng.random() < (A["wifi_tp_per_tick"] if drone else A["wifi_fp_per_tick"]) else 0.0))
    ev.append(("A", "ble", 0.95 if (not drone and rng.random() < A["ble_fp_per_tick"]) else 0.0))   # RID never present in this replay (conservative)
    for nd, s, sc in ev:
        meta = {}
        f.add(Detection(nd, s, sc, meta, t))
    return ev


def alarms(seq):
    """Count rising edges of a boolean series."""
    s = np.array(seq, bool); return int(((s[1:]) & (~s[:-1])).sum() + s[0])


if __name__ == "__main__":
    N = 3600
    f = Fusion(cfg["fusion"]); lv = []; per = {"acoustic": [], "vision": [], "wifi": []}
    for t in range(N):
        ev = tick(False, float(t), f)
        for nd, s, sc in ev:
            if s in per and nd == "A":
                per[s].append(sc >= 0.5)
        lv.append(f.step(float(t))["level"] == DRONE)
    # per-sensor: a naive 'alarm per positive tick' detector (what you'd get with one sensor + threshold)
    base = {s: {"alarm_ticks_per_hour": int(np.sum(v)), "alarm_events_per_hour": alarms(v)} for s, v in per.items()}
    fused_fp = alarms(lv)
    # drone present for 40 s: detection latency + miss rate over 1000 events
    lat, miss = [], 0
    for _ in range(1000):
        f = Fusion(cfg["fusion"]); hit = None
        for t in range(40):
            tick(True, float(t), f)
            if f.step(float(t))["level"] == DRONE and hit is None:
                hit = t
        lat.append(hit) if hit is not None else None
        miss += hit is None
    res = {"quiet_hour": {"per_sensor_alone": base, "fused_false_DRONE_alarms": fused_fp, "fused_alarm_ticks": int(sum(lv))},
           "drone_events": {"n": 1000, "missed": int(miss), "median_latency_s": float(np.median(lat)), "p95_latency_s": float(np.percentile(lat, 95))},
           "assumed": ASSUMED, "audio_neg_clips": int(len(AUD_NEG)), "audio_pos_clips": int(len(AUD_POS))}
    print(json.dumps(res, indent=1)); (ROOT / "results" / "fusion_eval.json").write_text(json.dumps(res, indent=1))
