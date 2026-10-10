"""python -m skyguard run            # hub (Node A) + dashboard; profile from config.toml / SKYGUARD_PROFILE
   python -m skyguard node --id B   # remote sensor node (UNO Q): posts to HUB_URL, drives its LED matrix"""
import argparse, logging, threading, time
from .core import load_config


def main():
    ap = argparse.ArgumentParser(prog="skyguard")
    ap.add_argument("cmd", choices=["run", "node"])
    ap.add_argument("--id", default="B"); ap.add_argument("--profile"); ap.add_argument("--port", type=int)
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if a.profile:
        import os; os.environ["SKYGUARD_PROFILE"] = a.profile
    cfg = load_config()
    if a.cmd == "run":
        import uvicorn
        from .server import create_app
        uvicorn.run(create_app(cfg), host=cfg["hub"]["host"], port=a.port or cfg["hub"]["port"], log_level="warning")
        return
    from .node import NodeRunner, HttpSink
    from . import hal
    import requests
    world = hal.sim.World(cfg["room"])
    roles = {"mic", "wifi", "ble", "camera", "vision"}
    node = NodeRunner(a.id, cfg, world, HttpSink(cfg["hub"]["url"]), roles).start()
    matrix = hal.build(cfg, world, a.id, {"matrix"}).get("matrix")
    while True:                       # poll hub level -> LED matrix (idle sweep / flashing warning)
        try:
            lvl = requests.get(cfg["hub"]["url"] + "/api/mini", timeout=3).json()["level"]
            matrix and matrix.set(lvl)
        except Exception as e:        # noqa: BLE001
            logging.getLogger("skyguard.node").debug("hub poll: %s", e)
        time.sleep(1)


main()
