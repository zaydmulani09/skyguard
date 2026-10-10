# 2-minute judge demo — SkyGuard

**Setup (before judges arrive):** dashboard open full-screen on the laptop (`http://192.168.77.2:8000` wired, or `python -m skyguard run` for the sim profile), phone showing the same page, Nest within earshot, UNO Q + LCD visible on the table.
The scripted demo is deterministic and runs on any profile, so **it cannot fail** — if hardware misbehaves, switch to `SKYGUARD_PROFILE=sim` and the exact same story plays.

| Time | Say | Do / what they see |
|---|---|---|
| **0:00** | "A hobby drone grounds a plane, or a firefighting helicopter. Commercial drone detectors cost tens of thousands. We built one for about **two hundred dollars**." | Point at the two nodes: Pi + webcam + LCD, UNO Q, Nest. |
| **0:15** | "SkyGuard never transmits — it's **fully passive**. It *hears* rotors, *sniffs* the drone's Wi-Fi and Bluetooth Remote ID, and *sees* it with a small YOLO model — on two nodes — then fuses all of it." | Scroll the header: *SIMULATED / LIVE* pill, node health both **online**. |
| **0:30** | "Watch a drone approach." | Click **▶ Run demo**. Gauge at 0 %, LCD green `CLEAR`, matrix sweeping. |
| **0:35** (t≈6 s) | "First, **sound**: the rotor's harmonic comb shows up in the spectrogram. One sensor isn't enough — we're only at *possible*." | Waterfall lights up harmonics; **AUDIO** chip on; gauge ~35–60 %, amber. |
| **0:50** (t≈17–20 s) | "Now **radio**: a `TELLO-…` network appears in the RF table, flagged, and its signal strength is *rising* — approaching. Two independent sensors agree → **DRONE DETECTED**." | Row turns red; **WI-FI RF** + **REMOTE ID** chips; LCD flips to flashing red `DRONE 9x%`; matrix flashes ⚠; **Nest speaks**. Whole page gets the red alarm frame. |
| **1:05** (t≈31 s) | "Third sensor — **vision**. The camera box gives a bearing, and the room map fuses both nodes' signal strength with that bearing: *north-east, approaching*." | Camera shows the drone with a red box; map zone **NE** outlined; trail + heat-map. |
| **1:20** | "Why trust it? **One sensor can never trigger an alarm.**" | Click **Stop / reset**, then **Test · Audio** → stays amber *POSSIBLE*, never red. Point at the ESP smart-plug row: amber 0.30, ignored. |
| **1:35** | "Over a replayed quiet hour, audio alone false-alarms about **100 times**; the fused system: **zero** — and still catches a drone in about one second." | Briefly show the README table (or `results/fusion_eval.json`). |
| **1:45** | "For the **security** track: node-to-hub traffic is HMAC-signed with replay protection, so a rogue device on the hotspot can't inject fake drones or mute real ones. It decodes **FAA Remote ID**, and it only ever listens." | Event log: snapshot, sources, zone. |
| **1:55** | "Honest limits: zone-level location, short acoustic range, not every drone talks on Wi-Fi. SkyGuard is a tripwire, not a weapon. Thank you." | Hold on the dashboard. |

## If something goes sideways
* **Nothing moves** → press **Stop / reset**, then **Run demo** again (it restarts from t = 0).
* **Hardware node offline** → `SKYGUARD_PROFILE=sim` in `.env`, `sudo systemctl restart skyguard-hub` (or run `python -m skyguard run` on the laptop). Same demo.
* **Nest silent** → the dashboard still shows "🔊 Nest: …" and the laptop-speaker fallback speaks.
* **Judge asks "is this real?"** → the *SIMULATED / LIVE* pill is honest. Hold a phone hotspot named `TELLO-ABC123` near the Pi, or play drone audio near the webcam mic, to show live RF / audio detection; say plainly which parts were validated on datasets vs. simulated.
* **Judge asks about accuracy** → audio: precision 0.97 / recall 0.77 / FPR 2.5 % on held-out recordings; localization: zone-level, ~1 m median when calibrated (simulated); vision: see README.

## One-sentence answers
* *Innovation:* three physically independent signals + two nodes + persistence-gated fusion at ≈ $200, with a Bayesian RSSI×camera localizer.
* *Theme:* "Into the Skies" — airspace awareness for airports, stadiums, prisons and wildfire zones.
* *Security:* passive by design, Remote ID decoding, signed telemetry, explicit false-positive engineering.
