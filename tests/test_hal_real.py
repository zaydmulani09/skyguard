"""Real-driver logic that can be verified without hardware: wire formats against fakes."""
import sys, types
from skyguard.hal import real


class FakeBus:
    def __init__(self, *a, **k): self.w = []; self.v5 = False
    def write_byte_data(self, addr, reg, val): self.w.append((addr, reg, val))
    def read_byte(self, addr):
        if addr == 0x30 and not self.v5: raise OSError
        return 0


def test_grove_lcd_v4_bytes(monkeypatch):
    monkeypatch.setitem(sys.modules, "smbus2", types.SimpleNamespace(SMBus=FakeBus))
    lcd = real.GroveLcd(); b = lcd.bus
    assert lcd.rgb == 0x62 and (0x62, 0x08, 0xFF) in b.w                         # v4.0 backlight chip init
    assert b.w[0] == (0x3E, 0x80, 0x28)                                           # function set 2-line
    b.w.clear(); lcd.show("DRONE 91% NE", "VIS+AUD+RF", "amber")
    txt = bytes(v for a, r, v in b.w if a == 0x3E and r == 0x40)
    assert txt == b"DRONE 91% NE    " + b"VIS+AUD+RF      "                         # two padded 16-char rows
    assert (0x3E, 0x80, 0xC0) in b.w                                              # cursor to line 2
    assert [(r, v) for a, r, v in b.w if a == 0x62][-3:] == [(0x04, 255), (0x03, 120), (0x02, 0)]   # amber = R,G,B regs 4,3,2


def test_unoq_matrix_rpc_frame(monkeypatch):
    import msgpack
    sent = []
    class S:
        def __init__(self, *a): pass
        def settimeout(self, t): pass
        def connect(self, p): sent.append(("connect", p))
        def sendall(self, b): sent.append(("send", b))
        def recv(self, n): return b""
        def close(self): pass
    monkeypatch.setattr(real.socket, "socket", S); monkeypatch.setattr(real.socket, "AF_UNIX", 1, raising=False)
    m = real.UnoQMatrix(); m.set("DRONE DETECTED"); m.set("DRONE DETECTED")        # second call is de-duplicated
    sends = [b for k, b in sent if k == "send"]
    assert len(sends) == 1 and msgpack.unpackb(sends[0]) == [0, 1, "matrix", [1]]   # msgpack-rpc request: method 'matrix', mode 1 = warning
    assert ("connect", "/var/run/arduino-router.sock") in sent


def test_nmcli_parser():
    esc = lambda mac: mac.replace(":", chr(92) + ":")           # nmcli -t escapes ':' inside fields
    out = esc("AA:BB:CC:DD:EE:FF") + ":TELLO-5A3F2C:62:1" + chr(10) + esc("11:22:33:44:55:66") + ":Home WiFi:80:11" + chr(10)
    aps = real._parse_nmcli(out)
    assert aps[0] == {"ssid": "TELLO-5A3F2C", "bssid": "AA:BB:CC:DD:EE:FF", "rssi": -69, "chan": 1}
    assert aps[1]["ssid"] == "Home WiFi" and aps[1]["rssi"] == -60


def test_rf_calibration_fit_recovers_model():
    import importlib.util, numpy as np
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("cal", Path(__file__).resolve().parent.parent / "scripts" / "calibrate_rf.py")
    cal = importlib.util.module_from_spec(spec); spec.loader.exec_module(cal)
    d = np.array([1, 3, 6, 10]); r = -48 - 10 * 3.1 * np.log10(d) + np.random.default_rng(0).normal(0, 0.5, 4)
    p0, n = cal.fit(d, r)
    assert abs(p0 + 48) < 2 and abs(n - 3.1) < 0.3
