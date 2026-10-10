"""Hardware abstraction layer: build(cfg, world, node) returns the driver set chosen by cfg['drivers'].
A driver that fails to initialise (no webcam, no BLE radio...) degrades to None and is logged: skip silently."""
from __future__ import annotations
import logging
from . import sim, real

log = logging.getLogger("skyguard.hal")


class NullOut:
    def show(self, *a, **k): pass
    def set(self, *a, **k): pass
    def say(self, *a, **k): pass


class SimLcd:
    """Prints the 16x2 + backlight to the log and keeps the last frame for the dashboard 'virtual LCD'."""
    last = ("", "", "green")

    def show(self, l1, l2, color="green"):
        self.last = (l1[:16].ljust(16), l2[:16].ljust(16), color)
        log.debug("LCD[%s] |%s|%s|", color, *self.last[:2])

    def set(self, level): pass


class SimMatrix:
    mode = "CLEAR"

    def set(self, level): self.mode = level


class SimSpeaker:
    spoken: list[str] = []

    def say(self, text):
        self.spoken.append(text); self.spoken[:] = self.spoken[-20:]; log.info("SPEAK: %s", text)


def _try(name, fn):
    try:
        return fn()
    except Exception as e:                                  # noqa: BLE001
        log.warning("driver %s unavailable (%s) -> disabled", name, e)
        return None


def build(cfg: dict, world, node: str, roles: set[str] | None = None) -> dict:
    """roles limits which devices this node owns; hub (A) owns lcd+speaker, B owns matrix."""
    d = cfg["drivers"]; roles = roles or {"camera", "mic", "wifi", "ble", "lcd", "matrix", "speaker", "vision"}
    out: dict = {}
    def want(k): return k in roles and d[k] != "none"
    if want("camera"):
        out["camera"] = sim.SimCamera(world, node, cfg) if d["camera"] == "sim" else _try("camera", lambda: real.WebcamCamera(cfg))
    if want("mic"):
        out["mic"] = (sim.SimMic(world, node, cfg) if d["mic"] == "sim" else
                      _try("mic", lambda: real.AlsaMic(cfg) if d["mic"] == "alsa" else real.SoundDeviceMic(cfg)))
    if want("wifi"):
        out["wifi"] = sim.SimWifi(world, node, cfg) if d["wifi"] == "sim" else _try("wifi", lambda: real.WifiScanner(cfg, d["wifi"]))
    if want("ble"):
        out["ble"] = sim.SimBle(world, node, cfg) if d["ble"] == "sim" else _try("ble", lambda: real.BleakScanner(cfg))
    if want("lcd") or (d["lcd"] == "sim" and "lcd" in roles):
        out["lcd"] = SimLcd() if d["lcd"] == "sim" else (_try("lcd", lambda: real.GroveLcd(cfg)) or SimLcd())
    if want("matrix") or (d["matrix"] == "sim" and "matrix" in roles):
        out["matrix"] = SimMatrix() if d["matrix"] == "sim" else (_try("matrix", lambda: real.UnoQMatrix(cfg)) or SimMatrix())
    if "speaker" in roles and d["speaker"] != "none":
        out["speaker"] = (SimSpeaker() if d["speaker"] == "sim" else
                          ((_try("speaker", lambda: real.NestSpeaker(cfg)) or real.LaptopSpeaker()) if d["speaker"] == "nest" else real.LaptopSpeaker()))
    if want("vision"):
        out["vision_kind"] = d["vision"]
    return out
