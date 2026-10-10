"""Real hardware drivers. All imports are lazy so the package runs anywhere. Everything is passive:
Wi-Fi uses the OS scan list (nmcli/iw/netsh) and BLE uses a listen-only scanner. No packets are injected."""
from __future__ import annotations
import re, shutil, socket, subprocess, sys, threading, time
import numpy as np

IS_WIN = sys.platform.startswith("win")


# ---- sensors ------------------------------------------------------------------
class WebcamCamera:
    def __init__(self, cfg: dict | None = None, index: int = 0):
        import cv2
        self.cv2 = cv2
        self.cap = cv2.VideoCapture(index, cv2.CAP_DSHOW) if IS_WIN else cv2.VideoCapture(index)
        if not self.cap.isOpened():
            raise RuntimeError("no webcam")
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640); self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    def read(self):
        ok, fr = self.cap.read()
        return fr if ok else None


class SoundDeviceMic:
    """Laptop / generic mic via sounddevice (PortAudio). On the Pi use AlsaMic if PortAudio is absent."""
    def __init__(self, cfg: dict | None = None, sr: int = 16000, device=None):
        import sounddevice as sd
        self.sd, self.sr, self.device = sd, sr, device
        sd.check_input_settings(device=device, channels=1, samplerate=sr)

    def read(self, seconds: float = 1.0) -> np.ndarray:
        x = self.sd.rec(int(seconds * self.sr), samplerate=self.sr, channels=1, dtype="float32", device=self.device)
        self.sd.wait()
        return x[:, 0]


class AlsaMic:
    """arecord pipe: works on a bare Pi with the Logitech USB webcam mic (default device or ALSA_DEVICE)."""
    def __init__(self, cfg: dict | None = None, sr: int = 16000, device: str | None = None):
        import os
        if not shutil.which("arecord"):
            raise RuntimeError("arecord missing (apt install alsa-utils)")
        self.sr, self.dev = sr, device or os.environ.get("ALSA_DEVICE", "default")

    def read(self, seconds: float = 1.0) -> np.ndarray:
        n = int(seconds * self.sr)
        out = subprocess.run(["arecord", "-q", "-D", self.dev, "-f", "S16_LE", "-r", str(self.sr), "-c", "1",
                              "-d", str(max(1, round(seconds))), "-t", "raw"], capture_output=True, timeout=seconds + 5).stdout
        x = np.frombuffer(out, dtype=np.int16).astype(np.float32) / 32768
        return x[:n] if len(x) >= n else np.pad(x, (0, n - len(x)))


def _parse_nmcli(txt: str) -> list[dict]:
    aps = []
    for ln in txt.strip().splitlines():
        parts = re.split(r"(?<!\\):", ln)                              # nmcli -t escapes ':' as '\:'
        if len(parts) < 4:
            continue
        bssid, ssid, sig, chan = parts[0].replace("\\", ""), parts[1], parts[2], parts[3]
        try:
            aps.append({"ssid": ssid.replace("\\", ""), "bssid": bssid, "rssi": int(int(sig) / 2 - 100), "chan": int(chan)})  # signal% -> dBm approx
        except ValueError:
            pass
    return aps


class WifiScanner:
    """Passive scan via the OS (no monitor mode, nothing transmitted beyond the NIC's normal probe requests).
    nmcli: Linux/NetworkManager. iw: raw 'iw dev wlan0 scan' (needs root). netsh: Windows laptop stand-in."""
    def __init__(self, cfg: dict | None = None, method: str = "auto", iface: str = "wlan0"):
        self.iface = iface
        self.method = method if method != "auto" else ("netsh" if IS_WIN else "nmcli" if shutil.which("nmcli") else "iw")

    def scan(self) -> list[dict]:
        if self.method == "nmcli":
            out = subprocess.run(["nmcli", "-t", "-f", "BSSID,SSID,SIGNAL,CHAN", "dev", "wifi", "list", "--rescan", "yes"],
                                 capture_output=True, text=True, timeout=20).stdout
            return _parse_nmcli(out)
        if self.method == "iw":
            out = subprocess.run(["iw", "dev", self.iface, "scan"], capture_output=True, text=True, timeout=20).stdout
            aps = []
            for blk in re.split(r"(?m)^BSS ", out)[1:]:
                m = re.match(r"([0-9a-f:]{17})", blk)
                sig = re.search(r"signal:\s*(-?\d+)", blk); ssid = re.search(r"SSID:\s*(.*)", blk)
                if m and sig:
                    aps.append({"ssid": ssid.group(1).strip() if ssid else "", "bssid": m.group(1).upper(), "rssi": int(sig.group(1)), "chan": 0})
            return aps
        out = subprocess.run(["netsh", "wlan", "show", "networks", "mode=bssid"], capture_output=True, text=True, timeout=20).stdout
        aps, ssid = [], ""
        for ln in out.splitlines():
            ln = ln.strip()
            if ln.startswith("SSID"):
                ssid = ln.split(":", 1)[1].strip()
            elif ln.startswith("BSSID"):
                aps.append({"ssid": ssid, "bssid": ln.split(":", 1)[1].strip().upper(), "rssi": -100, "chan": 0})
            elif ln.startswith("Signal") and aps:
                aps[-1]["rssi"] = int(int(ln.split(":")[1].strip("% ")) / 2 - 100)
        return aps


class BleakScanner:
    """Listen-only BLE advertisement scan (bleak). Collects service_data so Remote ID (0xFFFA) can be decoded."""
    def __init__(self, cfg: dict | None = None, seconds: float = 3.0):
        import bleak  # noqa: F401
        self.seconds = seconds

    def scan(self) -> list[dict]:
        import asyncio
        from bleak import BleakScanner as BS

        async def go():
            found = await BS.discover(timeout=self.seconds, return_adv=True)
            res = []
            for dev, adv in found.values():
                sd = {}
                for u, b in (adv.service_data or {}).items():
                    m = re.match(r"0000([0-9a-f]{4})-", u.lower())
                    if m:
                        sd[int(m.group(1), 16)] = bytes(b)
                res.append({"addr": dev.address, "name": adv.local_name or "", "rssi": adv.rssi, "service_data": sd})
            return res
        return asyncio.run(go())


# ---- outputs ------------------------------------------------------------------
class GroveLcd:
    """Grove LCD RGB Backlight 16x2 on I2C bus 1. Text ctrl @0x3E; RGB driver auto-detected (0x62 / 0x60 / 0x30)."""
    def __init__(self, cfg: dict | None = None, bus: int = 1):
        from smbus2 import SMBus
        self.bus = SMBus(bus)
        self.TXT = 0x3E
        self.rgb = self._detect_rgb()
        for c in (0x38, 0x39, 0x14, 0x70, 0x56, 0x6C):               # function set / osc / contrast / follower
            self._cmd(c); time.sleep(0.005)
        self._cmd(0x38); self._cmd(0x0C); self._cmd(0x01); self._cmd(0x06); time.sleep(0.005)
        self._flash, self._stop = None, threading.Event()
        self._init_rgb()

    def _detect_rgb(self):
        for a in (0x62, 0x60):
            try:
                self.bus.read_byte(a); return a
            except OSError:
                pass
        return 0x62

    def _cmd(self, c): self.bus.write_byte_data(self.TXT, 0x80, c)

    def _init_rgb(self):
        for reg, v in ((0x00, 0), (0x01, 0), (0x08, 0xAA)):
            self.bus.write_byte_data(self.rgb, reg, v)

    def color(self, r, g, b):
        for reg, v in ((0x04, r), (0x03, g), (0x02, b)):
            self.bus.write_byte_data(self.rgb, reg, v)

    def _text(self, l1: str, l2: str):
        self._cmd(0x01); time.sleep(0.003)
        for row, s in enumerate((l1, l2)):
            self._cmd(0x80 | (0x40 * row))
            for ch in s[:16].ljust(16):
                self.bus.write_byte_data(self.TXT, 0x40, ord(ch) & 0x7F)

    def show(self, line1: str, line2: str, color: str = "green"):
        self._text(line1, line2)
        self._stop.set()
        if color == "flash_red":
            self._stop = threading.Event()
            def blink(ev=self._stop):
                on = True
                while not ev.is_set():
                    self.color(255 if on else 20, 0, 0); on = not on; time.sleep(0.35)
            threading.Thread(target=blink, daemon=True).start()
        else:
            self.color(*{"green": (0, 160, 40), "amber": (255, 120, 0), "red": (255, 0, 0)}.get(color, (0, 0, 80)))


class UnoQMatrix:
    """UNO Q 8x13 matrix is owned by the STM32 MCU. The sketch (unoq_app/sketch) exposes Bridge.provide('matrix', mode)
    and we call it over the Arduino Router MessagePack-RPC unix socket /var/run/arduino-router.sock.
    Modes: 0=idle sweep, 1=warning icon (flashes in firmware), 2=possible."""
    SOCK = "/var/run/arduino-router.sock"

    def __init__(self, cfg: dict | None = None):
        import msgpack
        self.mp, self.msgid, self.last = msgpack, 0, None

    def _call(self, mode: int):
        s = socket.socket(socket.AF_UNIX); s.settimeout(3); s.connect(self.SOCK)
        self.msgid += 1
        s.sendall(self.mp.packb([0, self.msgid, "matrix", [mode]]))   # [type=request, msgid, method, params]
        try:
            s.recv(4096)
        finally:
            s.close()

    def set(self, level: str):
        mode = {"CLEAR": 0, "POSSIBLE": 2, "DRONE DETECTED": 1}.get(level, 0)
        if mode != self.last:
            try:
                self._call(mode); self.last = mode
            except OSError:           # router/sketch not up yet: retry on the next poll
                pass


class NestSpeaker:
    """TTS (piper -> gTTS -> none) to a WAV/MP3, served over local HTTP, cast to the Nest Mini with pychromecast."""
    def __init__(self, cfg: dict, tts_dir: str = "tts_cache", port: int = 8765):
        import pychromecast  # noqa: F401
        import os, http.server, functools
        self.cfg, self.dir, self.port = cfg, tts_dir, port
        os.makedirs(tts_dir, exist_ok=True)
        h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=tts_dir)
        h.log_message = lambda *a, **k: None
        self.httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), h)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.cast = None

    def _local_ip(self) -> str:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1)); return s.getsockname()[0]
        finally:
            s.close()

    def _connect(self):
        import pychromecast
        chromecasts, browser = pychromecast.get_listed_chromecasts(friendly_names=[self.cfg["nest"]["name"]], timeout=8)
        browser.stop_discovery()
        if not chromecasts:
            raise RuntimeError(f"Nest '{self.cfg['nest']['name']}' not found on LAN")
        self.cast = chromecasts[0]; self.cast.wait()

    def say(self, text: str):
        import hashlib, os
        fn = hashlib.md5(text.encode()).hexdigest()
        path = synth_tts(text, os.path.join(self.dir, fn), self.cfg)
        if self.cast is None:
            self._connect()
        mc = self.cast.media_controller
        mc.play_media(f"http://{self._local_ip()}:{self.port}/{os.path.basename(path)}",
                      "audio/wav" if path.endswith(".wav") else "audio/mpeg")
        mc.block_until_active(10)


def synth_tts(text: str, base: str, cfg: dict) -> str:
    """piper (local, offline) -> gTTS (online fallback). Returns the audio file path."""
    model = cfg.get("nest", {}).get("piper_model", "")
    if shutil.which("piper") and model:
        subprocess.run(["piper", "--model", model, "--output_file", base + ".wav"], input=text.encode(), check=True, timeout=30)
        return base + ".wav"
    from gtts import gTTS
    gTTS(text).save(base + ".mp3")
    return base + ".mp3"


class LaptopSpeaker:
    def say(self, text: str):
        if IS_WIN:
            ps = f"Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{text.replace(chr(39), '')}')"
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps])
        elif shutil.which("espeak"):
            subprocess.Popen(["espeak", text])
        elif shutil.which("say"):
            subprocess.Popen(["say", text])
