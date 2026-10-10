#!/usr/bin/env bash
# SkyGuard Node B (Arduino UNO Q) one-command deploy. Idempotent.
#   From the laptop:    ./deploy/unoq.sh        (needs UNOQ_HOST/UNOQ_USER/HUB_URL in .env)
#   On the UNO Q:       ./deploy/unoq.sh --on-device
# UNO Q = Qualcomm QRB2210 quad-A53 running Debian + STM32U585 MCU (owns the 8x13 LED matrix). Linux side does the radio
# scanning; the MCU sketch (unoq_app/) exposes Bridge.provide("matrix", mode) which we call over the Arduino Router socket.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ "${1:-}" != "--on-device" ]]; then
  [[ -f .env ]] || { echo "Missing .env — cp .env.example .env and fill it in"; exit 1; }
  set -a; source .env; set +a
  : "${UNOQ_HOST:?set UNOQ_HOST in .env}"; : "${HUB_URL:?set HUB_URL in .env (http://<Pi IP>:8000)}"; UNOQ_USER="${UNOQ_USER:-arduino}"
  SSH_OPTS="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=8"
  ssh $SSH_OPTS "${UNOQ_USER}@${UNOQ_HOST}" 'echo ok: $(hostname) $(uname -m)'
  tar --exclude=.venv --exclude=.git --exclude=__pycache__ --exclude='*.db' --exclude=snapshots --exclude=runs --exclude=models -czf - . \
    | ssh $SSH_OPTS "${UNOQ_USER}@${UNOQ_HOST}" 'mkdir -p ~/skyguard && tar -xzf - -C ~/skyguard'
  ssh -t $SSH_OPTS "${UNOQ_USER}@${UNOQ_HOST}" 'cd ~/skyguard && sed -i s/\x0d$// deploy/*.sh deploy/*.service .env && bash deploy/unoq.sh --on-device'
  exit 0
fi

APP="$PWD"; ME="$(id -un)"
say() { printf '\n==> %s\n' "$*"; }
[[ -f .env ]] || cp .env.example .env
grep -q '^SKYGUARD_PROFILE=' .env && sed -i 's/^SKYGUARD_PROFILE=.*/SKYGUARD_PROFILE=unoq/' .env || echo 'SKYGUARD_PROFILE=unoq' >> .env

say "apt packages (Debian on the UNO Q)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip network-manager bluez

say "python venv (Node B needs only numpy/requests/psutil/msgpack/bleak — no OpenCV, no YOLO)"
[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install -q -U pip
.venv/bin/pip install -q numpy requests psutil msgpack bleak scipy

say "LED-matrix app (flashes the STM32 sketch via arduino-app-cli)"
if command -v arduino-app-cli >/dev/null; then
  mkdir -p ~/ArduinoApps && rm -rf ~/ArduinoApps/skyguard-matrix && cp -r unoq_app ~/ArduinoApps/skyguard-matrix
  arduino-app-cli app start user:skyguard-matrix || echo "!! app start failed — open App Lab, import ~/ArduinoApps/skyguard-matrix and press Run once (see HARDWARE_SETUP.md)"
else
  echo "arduino-app-cli not found: open Arduino App Lab, create an app, paste unoq_app/sketch/sketch.ino + python/main.py, Run once."
fi
ls -l /var/run/arduino-router.sock 2>/dev/null || echo "(arduino-router socket not present yet — matrix falls back to a no-op until the sketch app runs)"

say "systemd service"
sed "s|__USER__|$ME|g; s|__APP__|$APP|g" deploy/skyguard-node-b.service | sudo tee /etc/systemd/system/skyguard-node-b.service >/dev/null
sudo systemctl daemon-reload && sudo systemctl enable --now skyguard-node-b.service
sleep 4; systemctl is-active skyguard-node-b.service || { journalctl -u skyguard-node-b -n 30 --no-pager; exit 1; }
say "DONE. Node B is posting to $(grep ^HUB_URL .env | cut -d= -f2). Watch it: journalctl -u skyguard-node-b -f"
