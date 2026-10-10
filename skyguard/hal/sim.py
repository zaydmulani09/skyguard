"""Simulated world + simulated drivers. The sim drivers feed the REAL DSP / RF matcher / fusion code:
the microphone driver synthesizes audio, the Wi-Fi driver emits beacon-like AP lists, etc."""
from __future__ import annotations
import math, random, time
import numpy as np
from ..core import bearing_deg, dist
from ..detectors.rf import encode_remote_id_for_tests

SR = 16000
# (t, x, y) metres; room is 12x8, node A (1,1), node B (11,7). Drone arrives from far NE, hovers mid-room, leaves.
DEMO_PATH = [(0, 45, 35), (12, 28, 22), (22, 14, 10), (30, 9, 6), (42, 6, 4), (50, 9, 6), (60, 40, 30), (70, 60, 45)]
AMBIENT_APS = [  # benign neighbours: must NOT trigger. ESP_ plug is a deliberate weak look-alike.
    ("PHS Hack-A-Thon", "A4:2B:B0:11:22:01", -58), ("xfinitywifi", "9C:3D:CF:44:55:02", -71),
    ("Home-5G-7731", "F0:9F:C2:AA:BB:03", -77), ("DIRECT-roku-123", "02:1A:11:22:33:04", -66),
    ("ESP_3D1A7C", "24:6F:28:3D:1A:7C", -80), ("NETGEAR42", "C0:FF:D4:11:22:05", -84)]


class World:
    """Ground truth shared by sim drivers. scenario: 'idle' | 'demo'; force: {sensor: until_t} for test buttons."""
    def __init__(self, room: dict, seed: int | None = None):
        self.room = room
        self.A, self.B = tuple(room["node_a"]), tuple(room["node_b"])
        self.cam_heading, self.hfov = room["camera_heading_deg"], room["camera_hfov_deg"]
        self.scenario, self.t0 = "idle", time.time()
        self.force: dict[str, float] = {}
        self.rng = np.random.default_rng(seed)
        self.speed = 1.0
        self.p0, self.pln = room.get("p0_dbm", -50.0), room.get("path_loss_n", 2.7)

    def start_demo(self):
        self.scenario, self.t0 = "demo", time.time()

    def stop(self):
        self.scenario = "idle"; self.force.clear()

    def inject(self, sensor: str, seconds: float = 8.0):
        self.force[sensor] = time.time() + seconds

    def forced(self, sensor: str) -> bool:
        return self.force.get(sensor, 0) > time.time()

    def demo_time(self) -> float:
        return (time.time() - self.t0) * self.speed

    def drone_pos(self) -> tuple[float, float] | None:
        """Ground-truth drone position (None if not flying). Forced sensors place it where that sensor can see it."""
        if self.scenario == "demo":
            t = self.demo_time()
            if t > DEMO_PATH[-1][0]:
                self.scenario = "idle"; return None
            for (t0, x0, y0), (t1, x1, y1) in zip(DEMO_PATH, DEMO_PATH[1:]):
                if t0 <= t <= t1:
                    f = (t - t0) / (t1 - t0)
                    return (x0 + f * (x1 - x0) + 0.15 * math.sin(t * 2.1), y0 + f * (y1 - y0) + 0.15 * math.cos(t * 1.7))
        if self.force:
            return (6.5, 4.5) if any(self.forced(s) for s in self.force) else None
        return None

    def active(self, sensor: str) -> bool:
        return self.scenario == "demo" or self.forced(sensor)

    # -- physical models --
    def rssi(self, node: str, pos) -> float:
        p0, n = self.p0, self.pln
        a = self.A if node == "A" else self.B
        d = max(dist(a, pos), 0.5)
        inside = 0 <= pos[0] <= self.room["w"] and 0 <= pos[1] <= self.room["h"]
        return p0 - 10 * n * math.log10(d) - (0 if inside else 3) + float(self.rng.normal(0, 2.0))

    def audio_snr_db(self, node: str, pos) -> float:
        a = self.A if node == "A" else self.B
        return 8 - 17 * math.log10(max(dist(a, pos), 1) / 2)

    def in_cam(self, pos) -> tuple[bool, float, float]:
        """(visible, normalised x in [0,1], distance) for Node A's camera."""
        b = bearing_deg(self.A, pos); d = dist(self.A, pos)
        off = (b - self.cam_heading + 180) % 360 - 180
        return (abs(off) < self.hfov / 2 and d < 10.5), 0.5 + off / self.hfov, d


# ---- audio synthesis -----------------------------------------------------------
def synth_drone(seconds: float, snr_db: float, rng, f0: float = 185.0, sr: int = SR) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    f = f0 * (1 + 0.012 * np.sin(2 * np.pi * 0.7 * t + rng.uniform(0, 6)))        # RPM wobble
    ph = 2 * np.pi * np.cumsum(f) / sr
    x = sum((1 / k ** 0.8) * np.sin(k * ph + rng.uniform(0, 6)) for k in range(1, 13))
    x *= 1 + 0.15 * np.sin(2 * np.pi * 6 * t)                                      # blade-pass AM
    x = x / np.sqrt(np.mean(x ** 2))
    return x * 10 ** (snr_db / 20)


def synth_noise(seconds: float, rng, sr: int = SR, kind: str = "room") -> np.ndarray:
    n = int(seconds * sr)
    w = rng.normal(0, 1, n)
    if kind == "room":                                                              # brown-ish hum + hiss
        spec = np.fft.rfft(w); fr = np.fft.rfftfreq(n, 1 / sr); spec /= np.maximum(fr, 40) ** 0.7
        w = np.fft.irfft(spec, n)
    return w / np.sqrt(np.mean(w ** 2))


def synth_speech(seconds: float, rng, sr: int = SR) -> np.ndarray:
    """Glottal pulses with a gliding f0 through formant resonances: harmonic but unsteady, unlike rotors."""
    n = int(seconds * sr); t = np.arange(n) / sr
    f0 = 120 + 40 * np.sin(2 * np.pi * 1.3 * t) + 25 * np.sin(2 * np.pi * 3.1 * t + 1)
    ph = np.cumsum(2 * np.pi * f0 / sr)
    x = sum(np.sin(k * ph) / k for k in range(1, 30))
    from scipy.signal import lfilter, butter
    for fc in (600 + 200 * np.sin(2 * np.pi * 2 * t[0]), 1500, 2500):
        b, a = butter(2, [max(fc - 150, 50) / (sr / 2), min(fc + 150, 7900) / (sr / 2)], "band")
        x = x + 0.8 * lfilter(b, a, x)
    x *= (np.sin(2 * np.pi * 3.5 * t) > -0.3)
    return x / (np.sqrt(np.mean(x ** 2)) + 1e-9)


class SimMic:
    def __init__(self, world: World, node: str, cfg: dict):
        self.w, self.node, self.rng = world, node, np.random.default_rng(hash(node) & 0xFFFF)

    def read(self, seconds: float = 1.0) -> np.ndarray:
        x = 0.3 * synth_noise(seconds, self.rng)
        pos = self.w.drone_pos()
        if pos is not None and self.w.active("acoustic") or self.w.forced("acoustic"):
            pos = pos or (6.5, 4.5)
            x = x + 0.3 * synth_drone(seconds, self.w.audio_snr_db(self.node, pos), self.rng, 185 + (8 if self.node == "B" else 0))
        elif self.rng.random() < 0.08:                                              # random benign clutter
            x = x + 0.25 * (synth_speech(seconds, self.rng) if self.rng.random() < .6 else synth_noise(seconds, self.rng, kind="white"))
        return x.astype(np.float32)


class SimCamera:
    W, H = 640, 360

    def __init__(self, world: World, node: str, cfg: dict):
        import cv2
        self.w = world
        yy = np.linspace(0, 1, self.H)[:, None, None]
        sky = (np.array([190, 140, 90]) * (1 - yy) + np.array([235, 215, 170]) * yy).astype(np.uint8)  # BGR gradient
        self.bg = np.repeat(sky, self.W, 1).copy()
        rng = np.random.default_rng(3)
        for i in range(0, self.W, 40):                                              # skyline
            h = int(rng.uniform(40, 130)); cv2.rectangle(self.bg, (i, self.H - h), (i + 36, self.H), (60, 55, 50), -1)
        self.truth: list[dict] = []

    def read(self) -> np.ndarray:
        import cv2
        fr = self.bg.copy()
        self.truth = []
        pos = self.w.drone_pos()
        if pos is not None and self.w.active("vision"):
            vis, nx, d = self.w.in_cam(pos)
            if self.w.forced("vision"):
                vis, nx, d = True, 0.5, 6.0
            if vis:
                size = int(np.clip(110 / max(d, 1.5) * 1.3, 14, 90)); cx = int(nx * self.W)
                cy = int(self.H * 0.35 + 8 * math.sin(time.time() * 3))
                for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
                    cv2.line(fr, (cx, cy), (cx + dx * size // 2, cy + dy * size // 3), (30, 30, 30), max(2, size // 14))
                    cv2.circle(fr, (cx + dx * size // 2, cy + dy * size // 3), size // 6, (25, 25, 25), -1)
                cv2.circle(fr, (cx, cy), size // 7, (20, 20, 20), -1)
                self.truth = [{"x": cx - size // 2 - 6, "y": cy - size // 3 - 6, "w": size + 12, "h": 2 * size // 3 + 12}]
        # sensor noise so frames aren't static
        fr = cv2.add(fr, np.random.randint(0, 6, fr.shape, dtype=np.uint8))
        return fr


class SimVision:
    """Sim 'detector' for the sim camera: ground-truth boxes + jitter/dropout/false positives."""
    def __init__(self, cam: SimCamera, cfg: dict):
        self.cam, self.rng = cam, np.random.default_rng(7)

    def detect(self, frame):
        boxes = []
        for b in self.cam.truth:
            if self.rng.random() > 0.1:
                j = self.rng.normal(0, 2, 4)
                boxes.append({"x": b["x"] + j[0], "y": b["y"] + j[1], "w": b["w"] + j[2], "h": b["h"] + j[3],
                              "conf": float(np.clip(self.rng.normal(0.86, 0.05), 0.5, 0.99))})
        if not boxes and self.rng.random() < 0.015:                                  # lone low-conf false positive (bird)
            boxes.append({"x": 300, "y": 90, "w": 22, "h": 14, "conf": 0.33})
        return boxes


class SimWifi:
    def __init__(self, world: World, node: str, cfg: dict):
        self.w, self.node, self.rng = world, node, np.random.default_rng(11)

    def scan(self) -> list[dict]:
        off = 0 if self.node == "A" else -3
        aps = [{"ssid": s, "bssid": b, "rssi": r + off + int(self.rng.normal(0, 2)), "chan": 6} for s, b, r in AMBIENT_APS]
        pos = self.w.drone_pos()
        if pos is not None and self.w.active("wifi"):
            if self.w.forced("wifi") and self.w.scenario != "demo":
                pos = (6.5, 4.5)
            r = self.w.rssi(self.node, pos)
            if r > -88:
                aps.append({"ssid": "TELLO-5A3F2C", "bssid": "60:60:1F:5A:3F:2C", "rssi": int(r), "chan": 1})
        return [a for a in aps if a["rssi"] > -90]


class SimBle:
    def __init__(self, world: World, node: str, cfg: dict):
        self.w, self.node, self.rng = world, node, np.random.default_rng(13)

    def scan(self) -> list[dict]:
        out = [{"addr": "5C:AA:11:22:33:44", "name": "Pixel Buds", "rssi": -62, "service_data": {}}]
        pos = self.w.drone_pos()
        if pos is not None and self.w.active("ble"):
            if self.w.forced("ble") and self.w.scenario != "demo":
                pos = (6.5, 4.5)
            d = dist(self.w.A if self.node == "A" else self.w.B, pos)
            if d < 14:
                payload = encode_remote_id_for_tests("1581F4XYZ0A1234", 33.7701 + pos[1] * 1e-5, -84.38 + pos[0] * 1e-5,
                                                     38.0, 3.5, int(bearing_deg(self.w.A, pos)))
                out.append({"addr": "60:60:1F:AB:CD:EF", "name": "", "rssi": int(-55 - 20 * math.log10(max(d, 1))),
                            "service_data": {0xFFFA: payload}})
        return out
