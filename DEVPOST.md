# SkyGuard — Devpost draft

**Tagline:** A ~$200, fully passive drone tripwire that hears, sniffs and sees — and only raises the alarm when its senses agree.

**Built for:** hackPHS 2026 · theme *Into the Skies* · Best Cybersecurity

## Inspiration
One hobby drone near a runway grounds aircraft; one in a wildfire zone grounds the helicopters fighting it. Stadiums and prisons fight the same problem. Counter-drone *detection* gear is sold to governments at government prices, and the cheap alternatives (a single camera, a single Wi-Fi sniffer) either miss most drones or cry wolf all day. We wanted a tripwire a school, a venue or a volunteer fire crew could actually afford — and we wanted it to be *safe by construction*: detection only, never jamming.

## What it does
Two sensor nodes — a **Raspberry Pi 4** (camera, mic, Wi-Fi/BLE, LCD, web console) and an **Arduino UNO Q** (Wi-Fi/BLE + LED matrix) — look for a drone three independent ways:
* **Sound:** an FFT "harmonic comb" detector finds the steady integer harmonics of rotor blade-pass frequency, and rejects speech, fans and music.
* **Radio:** passive Wi-Fi scans (no monitor mode) match drone SSIDs and vendor MAC prefixes and track signal strength over time (approaching vs. leaving); a BLE scan decodes **FAA/ASTM Remote ID** broadcasts (bonus).
* **Vision:** a YOLOv8-nano model fine-tuned on public drone images, with a tracker and a camera-FOV bearing.

A **fusion engine** weights and time-windows all of it, rewards cross-sensor and cross-node agreement, and refuses to let any single sensor declare a drone. A **Bayesian grid localizer** combines both nodes' signal strengths (plus the camera bearing) into a room zone ("north-east, approaching"). Alerts go to a Grove **LCD** (green/amber/flashing red), the UNO Q **LED matrix**, a **Nest Mini** that speaks ("Drone detected, north east, confidence 91 percent"), and a phone-friendly **web console** with live camera, spectrogram, RF table, room map, gauge, timeline and an event log with snapshots.

## How we built it
Python (FastAPI + WebSocket, NumPy/SciPy, OpenCV, Ultralytics, SQLite). Every device has a real and a simulated driver behind a hardware-abstraction layer, so the *whole system runs end to end with no hardware* and switches to the real Pi/UNO Q with one config line. Deployment is one command per node (`deploy/pi.sh`, `deploy/unoq.sh`: I²C, deps, services, Nest discovery). We tuned and evaluated the acoustic detector on the public Al-Emadi drone-audio dataset (split by recording), fine-tuned the vision model on a public drone dataset, and measured localization and fusion by simulation. 25 automated tests.

## Numbers (real, reproducible — see README)
* Acoustic, held-out recordings: **precision 0.97, recall 0.77, false-positive rate 2.5 %** (AUC 0.95).
* Fusion, replayed quiet hour: audio alone **100 false alarms**, vision alone 36, Wi-Fi alone 8 → **0 fused**; 1 000 simulated drone events: 0 missed, median time-to-alert **1 s**.
* Localization (simulated 12×8 m room): median error **0.8–1.2 m** with a calibrated model, 2–2.7 m with a mis-calibrated one (algorithm simulation, zone-level in practice).

## Challenges
Making false positives *structurally* hard (persistence + agreement + single-sensor lock-out) instead of tuning thresholds; two-anchor RSSI has a mirror ambiguity that we resolved with node placement on one wall, an outside-room prior and the camera bearing; deciding not to announce a direction we aren't sure of; and doing it all without hardware in hand — which pushed us to build the simulation honestly (real DSP on synthetic audio, real matcher on synthetic beacons).

## What we learned
RSSI is noisy but its *trend* is robust; agreement between cheap independent sensors beats one expensive one; and a "simulated" mode only builds trust if it runs the same code as the real one and says so on screen.

## Cybersecurity angle
Passive by design (legal and safe: no jamming, deauth or injection); decodes Remote ID; node→hub telemetry is HMAC-SHA256 signed with a replay window so a rogue device on a shared hotspot can't forge or silence detections; false-positive engineering is treated as a security property.

## What's next
Field-test with real drones; a Remote-ID-vs-observed-position spoofing check; more RSSI anchors for metre-level fixes; acoustic arrays for bearing; an on-device calibration walk-through.

## Built with
Python · FastAPI · WebSockets · NumPy/SciPy · OpenCV · Ultralytics YOLOv8 · ONNX · SQLite · Raspberry Pi 4 · Arduino UNO Q · Grove · Google Nest (pychromecast) · Piper TTS · systemd

## Links
GitHub: https://github.com/zaydmulani09/skyguard
