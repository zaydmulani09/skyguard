"""Passive RF: Wi-Fi SSID/OUI matcher, RSSI trend tracker, BLE Remote ID (ASTM F3411) decoder.
Nothing here transmits: scans only read beacons/advertisements the radios already hear."""
from __future__ import annotations
import re, struct, time
from collections import defaultdict, deque
import numpy as np

# (regex, score, label) -- first/best match wins. Score = P(drone | SSID alone), hand-set, see README.
SSID_RULES = [
    (r"^TELLO-[0-9A-F]{6}$", 0.97, "DJI/Ryze Tello"),
    (r"^(DJI|MAVIC|PHANTOM|SPARK|MINI\s?\d?|AIR\s?\d)[-_ ]?[0-9A-Z]*", 0.90, "DJI"),
    (r"^(ANAFI|BEBOP2?|DISCO)[-_]?", 0.92, "Parrot"),
    (r"^(AUTEL|EVO)[-_ ]", 0.90, "Autel"),
    (r"^(SKYDIO|SKYVIPER|HUBSAN|EACHINE|JJRC|SYMA|POTENSIC|HOLY\s?STONE|HS\d{3})", 0.85, "Toy/FPV maker"),
    (r"^(WIFI[_-]?UFO|KY[-_]?UFO|RC[-_]?UFO|UFO[-_]|WIFI[-_]?CAMERA[-_]?DRONE)", 0.70, "generic Wi-Fi drone"),
    (r"(^|[-_ ])FPV([-_ ]|$)|FPV[-_]?[0-9A-F]{4}", 0.65, "FPV"),
    (r"drone|quadcopter|uav", 0.60, "keyword"),
    (r"^(ESP[-_]?[0-9A-F]{6}|ESP(32|8266)[-_ ]?\w*|ESP_\w+)$", 0.30, "ESP default AP (weak)"),
]
# Manufacturer OUIs (partial, public IEEE list; extend with data/oui_drone.txt). Locally-administered MACs are ignored.
DRONE_OUIS = {
    "60:60:1F": "DJI", "34:D2:62": "DJI", "48:1C:B9": "DJI", "9C:5A:44": "DJI", "E4:7A:2C": "DJI",
    "04:A8:5A": "DJI", "0C:9A:E6": "DJI", "58:B8:58": "DJI", "D4:6A:6A": "DJI", "F4:F9:51": "DJI",
    "90:03:B7": "Parrot", "A0:14:3D": "Parrot", "00:12:1C": "Parrot", "00:26:7E": "Parrot", "90:3A:E6": "Parrot",
    "38:1D:14": "Skydio", "2C:AB:A4": "Autel",
}
_RULES = [(re.compile(p, re.I), s, lab) for p, s, lab in SSID_RULES]


def oui_of(mac: str) -> str | None:
    m = mac.upper().replace("-", ":")
    if len(m) < 8 or int(m[1], 16) & 2:        # locally administered / randomized -> no vendor info
        return None
    return DRONE_OUIS.get(m[:8])


def classify_ap(ssid: str, bssid: str = "") -> tuple[float, str]:
    """Return (score 0..1, reason). OUI+SSID agreement raises confidence (noisy-OR)."""
    s_ssid, why = 0.0, ""
    for rx, sc, lab in _RULES:
        if rx.search(ssid or ""):
            s_ssid, why = sc, f"ssid:{lab}"
            break
    v = oui_of(bssid) if bssid else None
    s_oui = 0.6 if v else 0.0
    score = 1 - (1 - s_ssid) * (1 - s_oui)
    if v:
        why = (why + " + " if why else "") + f"oui:{v}"
    return round(score, 3), why


class RssiTracker:
    """Per-BSSID RSSI history -> smoothed value + slope (dB/s). Positive slope = approaching."""
    def __init__(self, horizon_s: float = 20.0):
        self.h = horizon_s
        self.hist: dict[str, deque] = defaultdict(deque)

    def add(self, key: str, rssi: float, t: float | None = None):
        t = t or time.time()
        q = self.hist[key]
        q.append((t, rssi))
        while q and t - q[0][0] > self.h:
            q.popleft()

    def state(self, key: str) -> dict:
        q = self.hist.get(key)
        if not q:
            return {"rssi": None, "slope": 0.0, "trend": "unknown"}
        t = np.array([a for a, _ in q]); r = np.array([b for _, b in q])
        sm = float(np.median(r[-5:]))
        slope = float(np.polyfit(t - t[0], r, 1)[0]) if len(q) >= 4 and t[-1] - t[0] > 3 else 0.0
        trend = "approaching" if slope > 0.35 else "leaving" if slope < -0.35 else "steady"
        return {"rssi": sm, "slope": round(slope, 2), "trend": trend}


# ---- BLE Remote ID (ASTM F3411-22a / FAA / ASD-STAN) ---------------------------
RID_UUID16 = 0xFFFA
UA_TYPES = {0: "None", 1: "Aeroplane", 2: "Helicopter/Multirotor", 3: "Gyroplane", 4: "VTOL", 5: "Ornithopter",
            6: "Glider", 7: "Kite", 8: "Free balloon", 9: "Captive balloon", 10: "Airship", 11: "Unpowered",
            12: "Rocket", 13: "Tethered", 14: "Ground obstacle"}
ID_TYPES = {0: "None", 1: "Serial (ANSI/CTA-2063)", 2: "CAA registration", 3: "UTM UUID", 4: "Specific session"}


def decode_remote_id(service_data: bytes) -> dict | None:
    """Decode a BLE service-data payload for UUID 0xFFFA: [0x0D app code][counter][25-byte message...]."""
    if len(service_data) < 27 or service_data[0] != 0x0D:
        return None
    out: dict = {"counter": service_data[1]}
    off = 2
    while off + 25 <= len(service_data):
        m = service_data[off:off + 25]; off += 25
        mt = m[0] >> 4
        if mt == 0:                                  # Basic ID
            out["id_type"] = ID_TYPES.get(m[1] >> 4, "?")
            out["ua_type"] = UA_TYPES.get(m[1] & 0xF, "?")
            out["id"] = m[2:22].split(b"\0")[0].decode("ascii", "replace")
        elif mt == 1:                                # Location / vector
            flags = m[1] & 0xF
            direction = m[2] + (180 if flags & 1 else 0)
            spd = m[3] * (0.75 if flags & 2 else 0.25) + (255 * 0.25 if flags & 2 else 0)
            lat, lon = struct.unpack("<ii", m[5:13])
            alt_geo = struct.unpack("<H", m[15:17])[0] * 0.5 - 1000
            h_agl = struct.unpack("<H", m[17:19])[0] * 0.5 - 1000
            out.update(direction_deg=direction % 360, speed_ms=round(spd, 2), lat=lat * 1e-7, lon=lon * 1e-7,
                       alt_geo_m=alt_geo, height_agl_m=h_agl, status=m[1] >> 4)
        elif mt == 4:                                # System (operator location)
            lat, lon = struct.unpack("<ii", m[2:10])
            out.update(operator_lat=lat * 1e-7, operator_lon=lon * 1e-7)
        elif mt == 5:                                # Operator ID
            out["operator_id"] = m[2:22].split(b"\0")[0].decode("ascii", "replace")
    return out if len(out) > 1 else None


def encode_remote_id_for_tests(uas_id: str, lat: float, lon: float, h: float, speed: float, direction: int) -> bytes:
    """Builds a spec-shaped payload (used by the simulated BLE driver + unit tests)."""
    basic = bytes([0x00, (1 << 4) | 2]) + uas_id.encode()[:20].ljust(20, b"\0") + b"\0\0\0"
    flags = 1 if direction >= 180 else 0
    loc = bytearray(25); loc[0] = 0x10; loc[1] = (2 << 4) | flags; loc[2] = direction % 180
    loc[3] = int(speed / 0.25) & 0xFF
    loc[5:13] = struct.pack("<ii", int(lat * 1e7), int(lon * 1e7))
    for o, v in ((15, 0), (17, h)):
        loc[o:o + 2] = struct.pack("<H", int((v + 1000) / 0.5))
    return bytes([0x0D, 7]) + basic + bytes(loc)
