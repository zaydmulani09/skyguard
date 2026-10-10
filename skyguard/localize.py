"""Two-node RSSI localization (Bayesian grid filter).

Model (log-distance path loss):  RSSI_i = P0 - 10 n log10(d_i) + N(0, sigma^2)
Each cell c of a 0.25 m grid gets likelihood  prod_i exp(-(RSSI_i - model_i(c))^2 / 2 sigma^2).
Between updates the posterior diffuses (drone moves) -> predict step = Gaussian blur.
Optional camera bearing adds a wedge likelihood from Node A.  Output: 3x3 zone posterior, MAP
position, spread, and approaching/leaving from the RSSI slope.
Honest accuracy: with 2 anchors indoor RSSI the answer is ~zone level (+-2-3 m), NOT metres-precise;
two circles intersect in 2 points, so the mirror ambiguity is resolved only by the room bounds,
the motion prior and (when available) the camera bearing.
"""
from __future__ import annotations
import math
from collections import deque
import numpy as np
from scipy.ndimage import gaussian_filter
from .core import ZONES, zone_of, bearing_deg, compass


class Localizer:
    def __init__(self, room: dict, rf: dict, res: float = 0.25, sigma_db: float = 5.0, margin: float = 6.0):
        self.w, self.h = room["w"], room["h"]
        self.nodes = {"A": tuple(room["node_a"]), "B": tuple(room["node_b"])}
        self.cam_heading = room.get("camera_heading_deg", 0.0)
        self.hfov = room.get("camera_hfov_deg", 70.0)
        self.p0, self.n, self.sig, self.res = rf["p0_dbm"], rf["path_loss_n"], sigma_db, res
        self.m = margin                                    # grid extends beyond the walls: drones arrive from outside
        xs = np.arange(-margin + res / 2, self.w + margin, res); ys = np.arange(-margin + res / 2, self.h + margin, res)
        self.X, self.Y = np.meshgrid(xs, ys)               # [ny, nx]
        self.post = np.ones_like(self.X); self.post /= self.post.sum()
        self.prior = np.where((self.X >= 0) & (self.X <= self.w) & (self.Y >= 0) & (self.Y <= self.h), 1.0, 0.9)   # mild bias: mirror solution outside the room is less plausible
        self.zi = np.vectorize(lambda x, y: zone_of(x, y, self.w, self.h))(self.X, self.Y)   # outside cells clamp to the edge zone
        self.hist: dict[str, deque] = {"A": deque(maxlen=40), "B": deque(maxlen=40)}
        self.updates = 0
        self.heard, self.cam = 0, False

    # -- model ---
    def rssi_at(self, node: str, x, y):
        nx, ny = self.nodes[node]
        d = np.maximum(np.hypot(np.asarray(x) - nx, np.asarray(y) - ny), 0.5)
        return self.p0 - 10 * self.n * np.log10(d)

    def reset(self):
        self.post = np.ones_like(self.X) / self.X.size
        for q in self.hist.values():
            q.clear()
        self.updates = 0

    # -- update ---
    def update(self, rssi: dict[str, float | None], t: float = 0.0, cam_bearing: float | None = None, dt: float = 1.0):
        """rssi: smoothed RSSI per node (None = not heard). Returns posterior-summary dict."""
        # predict: drone moves ~ up to 3 m/s -> blur
        self.post = gaussian_filter(self.post, sigma=max(0.5, 1.0 * dt / self.res * 0.5), mode="constant")
        like = np.ones_like(self.post)
        heard = 0
        for nd, r in rssi.items():
            if r is None:
                continue
            heard += 1
            self.hist[nd].append((t, r))
            like *= np.exp(-0.5 * ((r - self.rssi_at(nd, self.X, self.Y)) / self.sig) ** 2)
        if cam_bearing is not None:                        # camera wedge from Node A (+-hfov/2 around bearing)
            ax, ay = self.nodes["A"]
            b = (np.degrees(np.arctan2(self.X - ax, self.Y - ay)) % 360)
            diff = (b - cam_bearing + 180) % 360 - 180
            like *= np.exp(-0.5 * (diff / (self.hfov / 3)) ** 2) + 1e-3
        if heard == 0:                                     # nothing heard -> slowly forget
            self.post = 0.9 * self.post + 0.1 / self.post.size
        else:
            self.post = self.post * like * self.prior + 1e-12
        self.post /= self.post.sum()
        self.updates += 1
        self.heard, self.cam = heard, cam_bearing is not None
        return self.summary()

    def trend(self) -> tuple[str, float]:
        """Approaching if combined RSSI slope (dB/s) over the last samples is clearly positive."""
        slopes = []
        for q in self.hist.values():
            if len(q) >= 5:
                ts = np.array([a for a, _ in q]); rs = np.array([b for _, b in q])
                if ts[-1] - ts[0] > 2:
                    slopes.append(np.polyfit(ts - ts[0], rs, 1)[0])
        if not slopes:
            return "unknown", 0.0
        s = float(np.mean(slopes))
        return ("approaching" if s > 0.3 else "leaving" if s < -0.3 else "steady"), round(s, 2)

    def summary(self) -> dict:
        p = self.post
        iy, ix = np.unravel_index(np.argmax(p), p.shape)
        mx, my = float(self.X[iy, ix]), float(self.Y[iy, ix])
        ex, ey = float((p * self.X).sum()), float((p * self.Y).sum())
        spread = float(math.sqrt((p * ((self.X - ex) ** 2 + (self.Y - ey) ** 2)).sum()))
        zp = {z: 0.0 for row in ZONES for z in row}
        zi = self.zi
        for z in zp:
            zp[z] = float(p[zi == z].sum())
        zone = max(zp, key=zp.get)
        tr, slope = self.trend()
        cx, cy = self.w / 2, self.h / 2
        return {"heard": self.heard, "cam": self.cam, "extent": [-self.m, -self.m, self.w + self.m, self.h + self.m], "inside_p": round(float(p[(self.X >= 0) & (self.X <= self.w) & (self.Y >= 0) & (self.Y <= self.h)].sum()), 3), "x": round(ex, 2), "y": round(ey, 2), "map_x": round(mx, 2), "map_y": round(my, 2),
                "spread_m": round(spread, 2), "zone": zone, "zone_p": round(zp[zone], 3),
                "compass": compass(bearing_deg((cx, cy), (ex, ey))) if math.hypot(ex - cx, ey - cy) > 0.5 else "C",
                "trend": tr, "slope_db_s": slope, "zones": {k: round(v, 3) for k, v in zp.items()}}

    def heatmap(self, step: int = 4) -> list[list[float]]:
        """Downsampled posterior, row 0 = north, normalised 0..1, for the dashboard."""
        q = self.post[::-1][::step, ::step]
        return (q / (q.max() + 1e-12)).round(2).tolist()
