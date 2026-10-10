"""Config, shared types, geometry helpers."""
from __future__ import annotations
import math, os, time, tomllib
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SENSORS = ("vision", "acoustic", "wifi", "ble")


def load_env(path: Path | None = None) -> None:
    """Tiny .env loader (no dependency). Real env wins."""
    p = path or ROOT / ".env"
    if p.exists():
        for ln in p.read_text().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def load_config(path: Path | None = None) -> dict:
    load_env()
    cfg = tomllib.loads((path or ROOT / "config.toml").read_text())
    cfg["profile"] = os.environ.get("SKYGUARD_PROFILE", cfg["profile"])
    cfg["drivers"] = cfg["profiles"][cfg["profile"]]
    if os.environ.get("HUB_URL"):
        cfg["hub"]["url"] = os.environ["HUB_URL"]
    if os.environ.get("NEST_NAME"):
        cfg["nest"]["name"] = os.environ["NEST_NAME"]
    return cfg


@dataclass
class Detection:
    """One detector output. score in [0,1]; meta is detector-specific."""
    node: str
    sensor: str                      # vision | acoustic | wifi | ble
    score: float
    meta: dict = field(default_factory=dict)
    t: float = field(default_factory=time.time)
    sim: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["meta"] = {k: (v.item() if hasattr(v, "item") else v) for k, v in self.meta.items()}
        d["score"] = round(float(self.score), 4)
        return d

    @staticmethod
    def from_dict(d: dict) -> "Detection":
        return Detection(d["node"], d["sensor"], float(d["score"]), d.get("meta", {}), d.get("t", time.time()), d.get("sim", False))


# ---- geometry ---------------------------------------------------------------
COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def compass(bearing_deg: float) -> str:
    return COMPASS[int(((bearing_deg % 360) + 22.5) // 45) % 8]


def bearing_deg(frm: tuple[float, float], to: tuple[float, float]) -> float:
    """Compass bearing (0=N, 90=E) from point to point, +y is north."""
    return math.degrees(math.atan2(to[0] - frm[0], to[1] - frm[1])) % 360


def dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


ZONES = [["NW", "N", "NE"], ["W", "C", "E"], ["SW", "S", "SE"]]  # row 0 = north


def zone_of(x: float, y: float, w: float, h: float) -> str:
    col = min(2, max(0, int(3 * x / w)))
    row = min(2, max(0, int(3 * (1 - y / h))))
    return ZONES[row][col]


def zone_center(name: str, w: float, h: float) -> tuple[float, float]:
    for r, row in enumerate(ZONES):
        if name in row:
            c = row.index(name)
            return ((c + .5) * w / 3, (1 - (r + .5) / 3) * h)
    return (w / 2, h / 2)
