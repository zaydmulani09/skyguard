"""Monte-Carlo accuracy of the 2-node RSSI localizer under realistic log-normal shadowing.
Truth: drone at a random point in the room (or hovering/approaching). Measurements: RSSI at A and B with sigma dB shadowing
+ model mismatch (path-loss exponent / P0 error). 20 RSSI samples (~1 per 0.5 s) per trial.
Usage: python scripts/eval_loc.py  -> results/loc_eval.json"""
import json, math, sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from skyguard.core import load_config, zone_of
from skyguard.localize import Localizer

cfg = load_config(); room, rf = cfg["room"], cfg["rf"]


def run(sigma, n_err=0.0, p0_err=0.0, trials=150, seed=0, cam=False):
    rng = np.random.default_rng(seed)
    L = Localizer(room, rf)
    errs, zhit, adj = [], 0, 0
    for _ in range(trials):
        L.reset()
        tx, ty = rng.uniform(0.5, room["w"] - 0.5), rng.uniform(0.5, room["h"] - 0.5)
        n_true, p0_true = rf["path_loss_n"] + n_err * rng.normal(), rf["p0_dbm"] + p0_err * rng.normal()
        for i in range(20):
            r = {}
            for nd in "AB":
                a = room["node_a"] if nd == "A" else room["node_b"]
                d = max(math.hypot(tx - a[0], ty - a[1]), 0.5)
                r[nd] = p0_true - 10 * n_true * math.log10(d) + rng.normal(0, sigma)
            bearing = None
            if cam:
                ax, ay = room["node_a"]; bearing = (math.degrees(math.atan2(tx - ax, ty - ay)) % 360) + rng.normal(0, 3)
            out = L.update(r, t=i * 0.5, cam_bearing=bearing, dt=0.5)
        errs.append(math.hypot(out["x"] - tx, out["y"] - ty))
        tz = zone_of(tx, ty, room["w"], room["h"])
        zhit += out["zone"] == tz
        adj += abs("NW N NE W C E SW S SE".split().index(out["zone"]) % 3 - "NW N NE W C E SW S SE".split().index(tz) % 3) <= 1 and \
               abs("NW N NE W C E SW S SE".split().index(out["zone"]) // 3 - "NW N NE W C E SW S SE".split().index(tz) // 3) <= 1
    e = np.array(errs)
    return {"sigma_db": sigma, "n_err": n_err, "p0_err_db": p0_err, "camera": cam, "median_err_m": round(float(np.median(e)), 2),
            "p90_err_m": round(float(np.percentile(e, 90)), 2), "zone_exact": round(zhit / trials, 3), "zone_within_1": round(adj / trials, 3)}


if __name__ == "__main__":
    rows = [run(s, ne, pe) for s, ne, pe in ((2, 0, 0), (4, 0, 0), (6, 0, 0), (4, 0.3, 3), (6, 0.5, 5))]
    rows.append(run(6, 0.5, 5, cam=True))
    for r in rows:
        print(r)
    Path("results").mkdir(exist_ok=True); Path("results/loc_eval.json").write_text(json.dumps(rows, indent=1))
