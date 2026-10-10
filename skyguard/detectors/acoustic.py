"""Acoustic drone detector: rotor harmonic-comb features + tiny logistic model.

Small-drone rotors emit a tonal blade-pass fundamental f0 (~100-500 Hz) plus strong
integer harmonics that are stable over time. Speech has harmonics too but f0 glides;
music is often wideband/tempo-varying; fans/wind are broadband (flat spectrum).
Features (per ~1 s clip):
  comb   : max over f0 of mean_k[ L(k f0) ] - mean_k[ L((k+.5) f0) ]  (dB, harmonic peakiness)
  nh     : number of harmonics (k<=10) that stand >3 dB above neighbouring valleys
  stab   : 1 / (1 + std of per-frame best-f0 / f0)   (pitch steadiness across 4 sub-frames)
  flat   : spectral flatness 100-4000 Hz (geometric/arithmetic mean)
  tilt   : dB energy 100-1000 Hz minus 1000-4000 Hz
  tonal  : peak-to-median ratio of log spectrum (dB)
score = sigmoid(w . z + b) with z standardized; weights learned by scripts/eval_audio.py.
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np

SR = 16000
NFFT = 4096
F0_GRID = np.arange(80.0, 520.0, 4.0)
BANDS = [(50, 150), (150, 300), (300, 600), (600, 1200), (1200, 2400), (2400, 4800), (4800, 7900)]
FEATS = ["comb", "nh", "stab", "flat", "tilt", "tonal", "comb_max_frame"] + [f"b{i}" for i in range(len(BANDS))] + ["cent"]


def _logspec(x: np.ndarray, sr: int, nfft: int = NFFT) -> tuple[np.ndarray, np.ndarray]:
    if len(x) < nfft:
        x = np.pad(x, (0, nfft - len(x)))
    # Welch-style average of Hann frames, 50% overlap
    w = np.hanning(nfft)
    hop = nfft // 2
    frames = [x[i:i + nfft] * w for i in range(0, len(x) - nfft + 1, hop)]
    p = np.mean([np.abs(np.fft.rfft(f)) ** 2 for f in frames], axis=0)
    f = np.fft.rfftfreq(nfft, 1 / sr)
    return f, 10 * np.log10(p + 1e-12)


def _comb(f: np.ndarray, L: np.ndarray, K: int = 8) -> tuple[float, float, int]:
    """Best harmonic-comb contrast (dB), its f0, harmonics standing out."""
    df = f[1] - f[0]
    best, bf0, bnh = -99.0, 0.0, 0
    for f0 in F0_GRID:
        ks = np.arange(1, K + 1)
        pk = (ks * f0 / df).round().astype(int)
        va = ((ks + .5) * f0 / df).round().astype(int)
        if va.max() + 2 >= len(L) or (ks * f0).max() > f[-1]:
            continue
        # peak = local max within +-1 bin (tolerates detune), valley = half-way mean
        pv = np.array([L[max(0, p - 1):p + 2].max() for p in pk])
        vv = np.array([L[max(0, v - 1):v + 2].mean() for v in va])
        c = float(np.mean(pv - vv))
        if c > best:
            best, bf0, bnh = c, float(f0), int(np.sum(pv - vv > 3.0))
    return best, bf0, bnh


def features(x: np.ndarray, sr: int = SR) -> dict:
    x = np.asarray(x, dtype=np.float64)
    if x.size == 0:
        return {k: 0.0 for k in FEATS}
    x = x - x.mean()
    rms = np.sqrt(np.mean(x ** 2)) + 1e-12
    x = x / rms                                      # level-invariant
    f, L = _logspec(x, sr)
    comb, f0, nh = _comb(f, L)
    # pitch steadiness across 4 sub-frames of 0.5 s (needs >= 1 NFFT each)
    sub = max(NFFT, len(x) // 4)
    f0s, combs = [], []
    for i in range(0, max(1, len(x) - sub + 1), max(1, sub // 2)):
        fs, Ls = _logspec(x[i:i + sub], sr, NFFT)
        c, ff0, _ = _comb(fs, Ls)
        f0s.append(ff0); combs.append(c)
    f0s = np.array(f0s)
    stab = 1.0 / (1.0 + (f0s.std() / max(f0, 1.0)) * 10) if len(f0s) > 1 else 0.5
    band = (f >= 100) & (f <= 4000)
    P = 10 ** (L[band] / 10)
    flat = float(np.exp(np.mean(np.log(P + 1e-12))) / (np.mean(P) + 1e-12))
    lo = np.mean(10 ** (L[(f >= 100) & (f < 1000)] / 10))
    hi = np.mean(10 ** (L[(f >= 1000) & (f <= 4000)] / 10)) + 1e-12
    tilt = float(10 * np.log10(lo / hi + 1e-12))
    tonal = float(np.max(L[band]) - np.median(L[band]))
    out = dict(comb=comb, nh=float(nh), stab=float(stab), flat=flat, tilt=tilt,
               tonal=tonal, comb_max_frame=float(max(combs)), f0=f0)
    Pw = 10 ** (L / 10)
    tot = Pw[(f >= 50) & (f <= 7900)].sum() + 1e-12
    for i, (lo_, hi_) in enumerate(BANDS):
        out[f"b{i}"] = float(10 * np.log10(Pw[(f >= lo_) & (f < hi_)].sum() / tot + 1e-9))   # band share of energy, dB
    m = (f >= 50) & (f <= 7900)
    out["cent"] = float((f[m] * Pw[m]).sum() / Pw[m].sum())
    return out


# default weights are overwritten by the trained model file (see scripts/eval_audio.py)
DEFAULT = {"mu": [0] * len(FEATS), "sd": [1] * len(FEATS), "w": [0.9, 0.4, 0.3, -0.8, 0.2, 0.3, 0.5] + [0.0] * 8, "b": -0.5}


class AcousticDetector:
    def __init__(self, model_path: str | Path | None = None):
        m = DEFAULT
        if model_path and Path(model_path).exists():
            m = json.loads(Path(model_path).read_text())
        self.mu, self.sd = np.array(m["mu"]), np.array(m["sd"])
        self.w, self.b = np.array(m["w"]), float(m["b"])
        self.thr = float(m.get("thr", 0.5))

    def score_features(self, ft: dict) -> float:
        z = (np.array([ft[k] for k in FEATS]) - self.mu) / self.sd
        return float(1 / (1 + np.exp(-(z @ self.w + self.b))))

    def score(self, x: np.ndarray, sr: int = SR) -> tuple[float, dict]:
        ft = features(x, sr)
        return self.score_features(ft), ft

    def calibrated(self, p: float) -> float:
        """Map classifier prob so the tuned operating threshold sits at 0.5 (fusion treats >0.5 as 'active')."""
        return float(1 / (1 + np.exp(-10 * (p - self.thr))))

    def detect(self, x: np.ndarray, sr: int = SR):
        p, ft = self.score(x, sr)
        s = self.calibrated(p)
        meta = {"prob": round(p, 3), "f0_hz": round(ft["f0"], 1), "harmonics": int(ft["nh"]), "comb_db": round(ft["comb"], 1),
                "flatness": round(ft["flat"], 3)}
        return s, meta

    @staticmethod
    def spectrogram(x: np.ndarray, sr: int = SR, bins: int = 64, frames: int = 24) -> list[list[int]]:
        """Small 0-255 log-magnitude spectrogram for the dashboard (0..4 kHz)."""
        n = 512; hop = max(1, (len(x) - n) // frames)
        w = np.hanning(n)
        rows = []
        for i in range(0, max(1, len(x) - n), hop)[:frames]:
            m = np.abs(np.fft.rfft(x[i:i + n] * w))
            m = m[: int(n * 4000 / sr)]
            idx = np.linspace(0, len(m) - 1, bins).astype(int)
            rows.append(10 * np.log10(m[idx] ** 2 + 1e-9))
        a = np.array(rows) if rows else np.zeros((1, bins))
        a = np.clip((a - a.max() + 70) / 70, 0, 1) * 255
        return a.astype(np.uint8).tolist()
