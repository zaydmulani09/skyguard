"""Tune + evaluate the acoustic detector on Al-Emadi DroneAudioDataset (Binary_Drone_Audio: yes_drone / unknown).
Split is BY SOURCE RECORDING (filename prefix) so near-identical neighbouring 1 s clips never straddle train/test.
Usage: python scripts/eval_audio.py <DroneAudioDataset dir> [--max-per-class N]
Writes models/acoustic.json and results/audio_eval.json."""
import argparse, json, re, sys, time
from collections import defaultdict
from pathlib import Path
import numpy as np, soundfile as sf
from concurrent.futures import ProcessPoolExecutor
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from skyguard.detectors.acoustic import features, FEATS

ROOT = Path(__file__).resolve().parent.parent


def feat_file(p):
    x, sr = sf.read(p, dtype="float32")
    if x.ndim > 1:
        x = x.mean(1)
    if sr != 16000:
        from scipy.signal import resample_poly
        x = resample_poly(x, 16000, sr)
    f = features(x, 16000)
    return [f[k] for k in FEATS]


def group(name: str) -> str:
    return re.sub(r"_\d+_?\.wav$", "", name)


def fit_logreg(X, y, l2=1e-2, iters=3000, lr=0.3):
    w = np.zeros(X.shape[1]); b = 0.0
    cw = {1: len(y) / (2 * y.sum()), 0: len(y) / (2 * (len(y) - y.sum()))}   # class-balanced
    sw = np.where(y == 1, cw[1], cw[0])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(X @ w + b)))
        g = (p - y) * sw
        w -= lr * (X.T @ g / len(y) + l2 * w); b -= lr * g.mean()
    return w, b


def metrics(y, p, thr):
    pred = p >= thr
    tp = int((pred & (y == 1)).sum()); fp = int((pred & (y == 0)).sum())
    fn = int((~pred & (y == 1)).sum()); tn = int((~pred & (y == 0)).sum())
    return dict(thr=round(float(thr), 3), tp=tp, fp=fp, fn=fn, tn=tn,
                precision=round(tp / max(tp + fp, 1), 4), recall=round(tp / max(tp + fn, 1), 4),
                fpr=round(fp / max(fp + tn, 1), 4), accuracy=round((tp + tn) / len(y), 4))


def auc(y, p):
    o = np.argsort(p); r = np.empty(len(p)); r[o] = np.arange(1, len(p) + 1)
    n1 = y.sum(); n0 = len(y) - n1
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("dir"); ap.add_argument("--max-per-class", type=int, default=2500)
    a = ap.parse_args()
    d = Path(a.dir) / "Binary_Drone_Audio"
    rng = np.random.default_rng(0)
    files, labels = [], []
    for lab, sub in ((1, "yes_drone"), (0, "unknown")):
        fs = sorted((d / sub).glob("*.wav"))
        fs = [fs[i] for i in rng.permutation(len(fs))[: a.max_per_class]]
        files += fs; labels += [lab] * len(fs)
    print(f"{len(files)} clips ({sum(labels)} drone)"); t0 = time.time()
    with ProcessPoolExecutor() as ex:
        X = np.array(list(ex.map(feat_file, files, chunksize=32)))
    y = np.array(labels); print(f"features {time.time()-t0:.0f}s, {1000*(time.time()-t0)*8/len(files):.1f} ms/clip/core-ish")
    grp = np.array([group(f.name) for f in files])
    ug = np.array(sorted(set(grp))); rng.shuffle(ug)
    test_g = set(ug[: max(1, int(0.3 * len(ug)))])
    te = np.array([g in test_g for g in grp]); tr = ~te
    mu, sd = X[tr].mean(0), X[tr].std(0) + 1e-9
    w, b = fit_logreg((X[tr] - mu) / sd, y[tr])
    p = lambda m: 1 / (1 + np.exp(-(((X[m] - mu) / sd) @ w + b)))
    ptr, pte = p(tr), p(te)
    # operating point: lowest FPR <= 2% on TRAIN with max recall
    cands = np.linspace(0.05, 0.95, 91)
    thr = next((t for t in cands if metrics(y[tr], ptr, t)["fpr"] <= 0.02), 0.5)
    res = {"n_train": int(tr.sum()), "n_test": int(te.sum()), "groups_train": len(ug) - len(test_g), "groups_test": len(test_g),
           "auc_test": round(auc(y[te], pte), 4), "test_at_thr": metrics(y[te], pte, thr), "test_at_0.5": metrics(y[te], pte, 0.5),
           "train_at_thr": metrics(y[tr], ptr, thr), "weights": dict(zip(FEATS, [round(float(v), 3) for v in w])), "bias": round(float(b), 3)}
    print(json.dumps(res, indent=1))
    (ROOT / "models").mkdir(exist_ok=True); (ROOT / "results").mkdir(exist_ok=True)
    np.savez(ROOT / "results" / "audio_test_scores.npz", y=y[te], p=pte, thr=thr)
    (ROOT / "models" / "acoustic.json").write_text(json.dumps({"mu": mu.tolist(), "sd": sd.tolist(), "w": w.tolist(), "b": float(b), "thr": float(thr), "feats": FEATS}))
    (ROOT / "results" / "audio_eval.json").write_text(json.dumps(res, indent=1))
