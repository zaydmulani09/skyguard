"""Fit the log-distance model RSSI = P0 - 10 n log10(d) to YOUR venue (the dominant localization error is a wrong P0/n).
Put a phone hotspot (any SSID) next to a node, run this ON that node, and follow the prompts (1 m, 3 m, 6 m, 10 m).
Usage: python scripts/calibrate_rf.py --ssid MyHotspot [--distances 1 3 6 10]   -> prints p0_dbm / path_loss_n for config.toml"""
import argparse, sys, time
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def fit(dists, rssis):
    """Least squares on x = 10 log10(d): RSSI = P0 - n x."""
    x = 10 * np.log10(np.asarray(dists, float))
    A = np.vstack([np.ones_like(x), -x]).T
    (p0, n), *_ = np.linalg.lstsq(A, np.asarray(rssis, float), rcond=None)
    return float(p0), float(n)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--ssid", required=True); ap.add_argument("--distances", type=float, nargs="+", default=[1, 3, 6, 10])
    ap.add_argument("--scans", type=int, default=6)
    a = ap.parse_args()
    from skyguard.hal.real import WifiScanner
    sc = WifiScanner(); ds, rs = [], []
    for d in a.distances:
        input(f"Place the hotspot {d} m from this node, then press Enter...")
        vals = []
        for _ in range(a.scans):
            vals += [ap_["rssi"] for ap_ in sc.scan() if ap_["ssid"] == a.ssid]; time.sleep(1)
        if not vals:
            print(f"  '{a.ssid}' not heard at {d} m — skipped"); continue
        ds.append(d); rs.append(float(np.median(vals))); print(f"  {d} m -> median {rs[-1]:.0f} dBm ({len(vals)} samples)")
    if len(ds) >= 2:
        p0, n = fit(ds, rs)
        print(f"\nAdd to config.toml [rf]:\np0_dbm = {p0:.1f}\npath_loss_n = {n:.2f}")
