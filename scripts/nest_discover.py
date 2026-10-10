"""List Chromecast / Nest devices on the LAN (mDNS, listen-only). --write-env stores NEST_NAME in .env.
Prefers a device whose name contains 'nest' or 'mini', else the only audio device found."""
import argparse, os, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--write-env", action="store_true"); ap.add_argument("--timeout", type=int, default=10)
    a = ap.parse_args()
    import pychromecast
    casts, browser = pychromecast.get_chromecasts(timeout=a.timeout)
    browser.stop_discovery()
    for c in casts:
        print(f"{c.name!r:32} model={c.model_name!r:22} {c.cast_info.host}:{c.cast_info.port}")
    if not casts:
        sys.exit("no cast devices found (same Wi-Fi/hotspot? client isolation off?)")
    pick = [c for c in casts if re.search(r"nest|mini|home", c.name + c.model_name, re.I)] or [c for c in casts if c.cast_type == "audio"] or casts
    if os.environ.get("NEST_NAME") and any(c.name == os.environ["NEST_NAME"] for c in casts):
        print("NEST_NAME already valid:", os.environ["NEST_NAME"]); sys.exit(0)
    if len(pick) >= 1 and a.write_env:
        env = ROOT / ".env"; txt = env.read_text() if env.exists() else ""
        line = f"NEST_NAME={pick[0].name}"
        txt = re.sub(r"(?m)^NEST_NAME=.*$", line, txt) if re.search(r"(?m)^NEST_NAME=", txt) else txt + ("\n" if txt and not txt.endswith("\n") else "") + line + "\n"
        env.write_text(txt); print("wrote", line)
