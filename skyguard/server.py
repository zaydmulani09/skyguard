"""Node A hub: ingest -> fusion + localization -> alerts + SQLite log + FastAPI/WebSocket dashboard."""
from __future__ import annotations
import asyncio, base64, contextlib, collections, json, logging, os, sqlite3, threading, time
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
import cv2, numpy as np
from .core import ROOT, Detection, SENSORS, load_config, compass as _c, zone_of
from .fusion import Fusion, source_tag, DRONE, POSSIBLE, CLEAR
from .localize import Localizer
from .node import NodeRunner
from . import hal

log = logging.getLogger("skyguard.hub")
LONG = {"N": "north", "NE": "north east", "E": "east", "SE": "south east", "S": "south", "SW": "south west", "W": "west", "NW": "north west", "C": "the centre"}
SCHEMA = """CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, t REAL, t_end REAL, level TEXT, conf REAL, sources TEXT,
zone TEXT, compass TEXT, snapshot TEXT, detail TEXT, sim INTEGER);
CREATE TABLE IF NOT EXISTS detections(t REAL, node TEXT, sensor TEXT, score REAL, meta TEXT, sim INTEGER);"""


def _np(o):
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


class Hub:
    def __init__(self, cfg: dict, node_id: str = "A"):
        self.cfg, self.id = cfg, node_id
        self.sim_profile = cfg["profile"] == "sim"
        self.fusion = Fusion(cfg["fusion"])
        self.loc = Localizer(cfg["room"], cfg["rf"])
        self.lock = threading.RLock()
        self.world = hal.sim.World({**cfg["room"], "p0_dbm": cfg["rf"]["p0_dbm"], "path_loss_n": cfg["rf"]["path_loss_n"]})
        self.db = sqlite3.connect(ROOT / cfg["hub"]["db"], check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.snapdir = ROOT / cfg["hub"]["snapshots"]; self.snapdir.mkdir(exist_ok=True)
        self.latest: dict[tuple, dict] = {}          # (node, sensor) -> detection dict
        self.rf: dict[tuple, dict] = {}              # (node, kind) -> rf message
        self.spec: dict[str, dict] = {}
        self.frames: dict[str, dict] = {}; self.frame_seq = 0
        self.health: dict[str, dict] = {}
        self.timeline = collections.deque(maxlen=600)
        self.cur_event: int | None = None
        self.last_speak, self.fres, self.lres = 0.0, {"confidence": 0, "level": CLEAR, "evidence": {k: 0.0 for k in SENSORS}, "sources": [], "cross_node": [], "z": 0}, {}
        self.demo_until = 0.0
        self.virtual: list[NodeRunner] = []
        self.nodes: list[NodeRunner] = []
        self.outputs = hal.build(cfg, self.world, node_id, {"lcd", "speaker", "matrix"})
        self.stop = threading.Event()
        self.counts = collections.Counter()

    # ---- lifecycle ----
    def start(self):
        if self.sim_profile:
            self._start_virtual()
        else:
            self.nodes.append(NodeRunner(self.id, self.cfg, self.world, self.ingest, {"camera", "mic", "wifi", "ble", "vision"}).start())
        threading.Thread(target=self._loop, daemon=True, name="fusion").start()
        return self

    def _start_virtual(self):
        if self.virtual:
            return
        for nid in ("A", "B"):
            sim_cfg = dict(self.cfg); sim_cfg["drivers"] = self.cfg["profiles"]["sim"]
            roles = {"camera", "mic", "wifi", "ble", "vision"} if nid == "A" else {"mic", "wifi", "ble"}
            self.virtual.append(NodeRunner(nid, sim_cfg, self.world, self.ingest, roles, is_sim=True).start())

    def _stop_virtual(self):
        for n in self.virtual:
            n.close()
        self.virtual.clear()

    # ---- ingest ----
    def ingest(self, msg: dict):
        if isinstance(msg, list):
            for m in msg:
                self.ingest(m)
            return
        typ = msg.get("type")
        sim = bool(msg.get("sim", False))
        if not self.sim_profile and sim != self.sim_active():   # real profile: real data normally, sim data only during demo/test
            return
        with self.lock:
            if typ == "detection":
                d = Detection.from_dict(msg)
                self.latest[(d.node, d.sensor)] = msg
                self.fusion.add(d) if d.score > 0.02 else None
                if d.score >= 0.5 and self.counts[(d.node, d.sensor)] % 5 == 0:
                    self.db.execute("INSERT INTO detections VALUES(?,?,?,?,?,?)", (d.t, d.node, d.sensor, d.score, json.dumps(d.meta), int(d.sim)))
                self.counts[(d.node, d.sensor)] += 1
            elif typ == "rf":
                self.rf[(msg["node"], msg["kind"])] = {**msg, "t": time.time()}
            elif typ == "spec":
                self.spec[msg["node"]] = msg
            elif typ == "frame":
                self.frames[msg["node"]] = msg; self.frame_seq += 1
            elif typ == "health":
                self.health[msg["node"]] = msg

    def sim_active(self) -> bool:
        return self.sim_profile or time.time() < self.demo_until or bool(self.world.force) and any(self.world.forced(s) for s in SENSORS)

    # ---- control ----
    def start_demo(self):
        self._start_virtual()
        self.world.start_demo(); self.demo_until = time.time() + 80
        self.fusion.events.clear(); self.fusion.level = CLEAR; self.loc.reset()

    def stop_demo(self):
        self.world.stop(); self.demo_until = 0
        if not self.sim_profile:
            self._stop_virtual()

    def test(self, sensor: str, seconds: float = 8.0):
        self._start_virtual()
        self.world.inject(sensor, seconds); self.demo_until = max(self.demo_until, time.time() + seconds + 3)

    # ---- main loop ----
    def _rssi_for_loc(self, now):
        out = {}
        for nd in ("A", "B"):
            d = self.latest.get((nd, "wifi"))
            out[nd] = d["meta"].get("rssi") if d and now - d["t"] < 6 and d["score"] > 0.5 else None
        return out

    def _loop(self):
        last_save = 0
        while not self.stop.wait(0.5):
            try:
                now = time.time()
                with self.lock:
                    if self.demo_until and now > self.demo_until and not self.sim_profile:
                        self.stop_demo()
                    f = self.fusion.step(now); self.fres = f
                    v = self.latest.get(("A", "vision"))
                    cam_b = v["meta"].get("bearing") if v and now - v["t"] < 3 and v["score"] > 0.5 else None
                    rs = self._rssi_for_loc(now)
                    if any(r is not None for r in rs.values()) or cam_b is not None or f["level"] != CLEAR:
                        self.lres = self.loc.update(rs, now, cam_b, 0.5)
                    elif self.loc.updates:
                        self.loc.reset(); self.lres = {}
                    self.timeline.append((round(now, 1), f["confidence"], f["level"]))
                    self._events(f, now)
                    self._outputs(f)
            except Exception:                                    # noqa: BLE001
                log.exception("loop")

    def _snapshot(self, now) -> str | None:
        fr = self.frames.get("A")
        if not fr:
            return None
        name = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}.jpg"
        (self.snapdir / name).write_bytes(base64.b64decode(fr["jpg"]))
        return name

    def _where(self):
        l = self.lres or {}
        return l.get("zone", "?"), l.get("compass", "?")

    def _events(self, f, now):
        lvl = f["level"]
        zone, comp = self._where()
        detail = json.dumps({"evidence": f["evidence"], "cross_node": f["cross_node"], "loc": {k: self.lres.get(k) for k in ("x", "y", "trend", "spread_m")} if self.lres else {}})
        if lvl == DRONE and self.cur_event is None:
            cur = self.db.execute("INSERT INTO events(t,level,conf,sources,zone,compass,snapshot,detail,sim) VALUES(?,?,?,?,?,?,?,?,?)",
                                  (now, lvl, f["confidence"], ",".join(f["sources"]), zone, comp, self._snapshot(now), detail, int(self.sim_active())))
            self.cur_event = cur.lastrowid; self.ev_src = set(f["sources"]); self.db.commit()
        elif self.cur_event is not None:
            if lvl == DRONE:
                self.ev_src |= set(f["sources"])          # union: every sensor that fired during the event
                self.db.execute("UPDATE events SET conf=max(conf,?), sources=?, zone=?, compass=?, detail=? WHERE id=?",
                                (f["confidence"], ",".join(s for s in SENSORS if s in self.ev_src), zone, comp, detail, self.cur_event))
            else:
                self.db.execute("UPDATE events SET t_end=? WHERE id=?", (now, self.cur_event)); self.cur_event = None
            self.db.commit()

    def lcd_lines(self, f):
        comp = self._where()[1]
        pct = int(round(f["confidence"] * 100))
        if f["level"] == DRONE:
            return f"DRONE {pct}% {comp if comp != '?' else ''}".strip(), source_tag(f["sources"]), "flash_red"
        if f["level"] == POSSIBLE:
            return f"POSSIBLE {pct}%", source_tag(f["sources"]) or "checking", "amber"
        return "CLEAR", "SkyGuard armed", "green"

    def _outputs(self, f):
        o = self.outputs
        l1, l2, col = self.lcd_lines(f)
        key = (l1, l2, col)
        if getattr(self, "_lastlcd", None) != key and "lcd" in o:
            o["lcd"].show(l1, l2, col); self._lastlcd = key
        if "matrix" in o:
            o["matrix"].set(f["level"])
        if f["level"] == DRONE and "speaker" in o and time.time() - self.last_speak > self.cfg["fusion"]["speak_cooldown_s"]:
            self.last_speak = time.time()
            comp = LONG.get(self._where()[1], "")
            txt = f"Drone detected{', ' + comp if comp else ''}, confidence {int(round(f['confidence'] * 100))} percent"
            threading.Thread(target=lambda: self._say(txt), daemon=True).start()

    def _say(self, txt):
        try:
            self.outputs["speaker"].say(txt)
        except Exception as e:                                   # noqa: BLE001
            log.warning("speaker: %s", e)
            if hasattr(hal.real.LaptopSpeaker, "say"):
                hal.real.LaptopSpeaker().say(txt)

    # ---- state for UI ----
    def snapshot_state(self) -> dict:
        now = time.time()
        with self.lock:
            f, l = self.fres, self.lres
            l1, l2, col = self.lcd_lines(f)
            rf = []
            for (nd, kind), m in self.rf.items():
                if now - m["t"] < 15:
                    rf += [{**a, "node": nd, "kind": kind} for a in m["aps"]]
            ev = [dict(zip(("id", "t", "t_end", "level", "conf", "sources", "zone", "compass", "snapshot", "sim"), r)) for r in
                  self.db.execute("SELECT id,t,t_end,level,conf,sources,zone,compass,snapshot,sim FROM events ORDER BY id DESC LIMIT 25")]
            truth = self.world.drone_pos() if self.sim_active() else None
            tl = list(self.timeline)[-240:]
            return {"t": now, "fusion": f, "loc": l, "heat": self.loc.heatmap() if l else None, "truth": truth,
                    "rf": sorted(rf, key=lambda r: (-r["score"] if "score" in r else 0, -r["rssi"]))[:30],
                    "spec": {k: v["rows"] for k, v in self.spec.items()}, "health": self.health,
                    "events": ev, "timeline": tl, "lcd": {"l1": l1, "l2": l2, "color": col},
                    "matrix": self.outputs["matrix"].mode if "matrix" in self.outputs and hasattr(self.outputs["matrix"], "mode") else f["level"],
                    "spoken": getattr(self.outputs.get("speaker"), "spoken", [])[-3:],
                    "profile": self.cfg["profile"], "simulated": self.sim_active(), "demo": self.world.scenario == "demo",
                    "demo_t": round(self.world.demo_time(), 1) if self.world.scenario == "demo" else None,
                    "vision_model": next((n.vis.model_name for n in self.virtual + self.nodes if n.vis), "n/a"),
                    "latest": {f"{k[0]}:{k[1]}": {"score": v["score"], "meta": v["meta"], "age": round(now - v["t"], 1)} for k, v in self.latest.items()},
                    "room": self.cfg["room"], "drivers": self.cfg["drivers"]}


def create_app(cfg: dict | None = None) -> FastAPI:
    cfg = cfg or load_config()
    hub = Hub(cfg)
    @contextlib.asynccontextmanager
    async def life(_):
        hub.start()
        yield
        hub.stop.set()
        for n in hub.nodes + hub.virtual:
            n.close()

    app = FastAPI(title="SkyGuard", lifespan=life)
    app.state.hub = hub

    web = ROOT / "web"
    app.mount("/snap", StaticFiles(directory=hub.snapdir), name="snap")

    @app.get("/")
    def index():
        return FileResponse(web / "index.html")

    @app.get("/api/state")
    def state():
        return Response(json.dumps(hub.snapshot_state(), default=_np), media_type="application/json")

    @app.get("/api/mini")
    def mini():  # polled by Node B for its LED matrix
        return {"level": hub.fres["level"], "confidence": hub.fres["confidence"]}

    @app.post("/api/ingest")
    async def ingest(req: Request):
        hub.ingest(await req.json())
        return {"ok": True}

    @app.post("/api/demo/{action}")
    def demo(action: str):
        hub.start_demo() if action == "start" else hub.stop_demo()
        return {"ok": True}

    @app.post("/api/test/{sensor}")
    def test(sensor: str):
        if sensor not in SENSORS:
            return JSONResponse({"error": "unknown sensor"}, 400)
        hub.test(sensor); return {"ok": True}

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        seq = -1
        try:
            n = 0
            while True:
                if hub.frame_seq != seq:
                    seq = hub.frame_seq
                    for fr in list(hub.frames.values()):
                        await sock.send_text(json.dumps({"type": "frame", **fr}))
                if n % 2 == 0:
                    await sock.send_text(json.dumps({"type": "state", **hub.snapshot_state()}, default=_np))
                n += 1
                await asyncio.sleep(0.25)
        except (WebSocketDisconnect, RuntimeError):
            pass

    return app
