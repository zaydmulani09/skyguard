#!/usr/bin/env bash
# SkyGuard Node A (Raspberry Pi 4) one-command deploy. Idempotent: safe to re-run.
#   From the laptop:   ./deploy/pi.sh            (reads .env, syncs repo, runs setup on the Pi over SSH)
#   On the Pi itself:  ./deploy/pi.sh --on-device
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ "${1:-}" != "--on-device" ]]; then
  [[ -f .env ]] || { echo "Missing .env — cp .env.example .env and fill it in"; exit 1; }
  set -a; source .env; set +a
  : "${PI_HOST:?set PI_HOST in .env}"; PI_USER="${PI_USER:-pi}"
  if [[ -z "${INGEST_KEY:-}" ]]; then            # shared HMAC secret so only our nodes can feed the hub
    INGEST_KEY="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' 
')"
    grep -q '^INGEST_KEY=' .env && sed -i "s|^INGEST_KEY=.*|INGEST_KEY=$INGEST_KEY|" .env || echo "INGEST_KEY=$INGEST_KEY" >> .env
    echo "==> generated INGEST_KEY in .env (reused by unoq.sh)"
  fi
  SSH_OPTS="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=8"
  echo "==> reaching ${PI_USER}@${PI_HOST}"
  ssh $SSH_OPTS "${PI_USER}@${PI_HOST}" 'echo ok: $(hostname) $(uname -m)'
  echo "==> syncing repo (tar over ssh; no rsync needed)"
  tar --exclude=.venv --exclude=.git --exclude=__pycache__ --exclude='*.db' --exclude=snapshots --exclude=runs --exclude=tts_cache -czf - . \
    | ssh $SSH_OPTS "${PI_USER}@${PI_HOST}" 'mkdir -p ~/skyguard && tar -xzf - -C ~/skyguard'
  echo "==> running setup on the Pi (sudo password may be asked once)"
  ssh -t $SSH_OPTS "${PI_USER}@${PI_HOST}" 'cd ~/skyguard && sed -i "s/$//" deploy/*.sh deploy/*.service .env && bash deploy/pi.sh --on-device'
  echo; echo "Dashboard:  http://${PI_HOST}:8000   (also reachable over the hotspot IP)"
  exit 0
fi

# ------------------------------- on the Pi -------------------------------------
APP="$PWD"; ME="$(id -un)"
say() { printf '\n==> %s\n' "$*"; }
[[ -f .env ]] || cp .env.example .env
grep -q '^SKYGUARD_PROFILE=' .env && sed -i 's/^SKYGUARD_PROFILE=.*/SKYGUARD_PROFILE=pi/' .env || echo 'SKYGUARD_PROFILE=pi' >> .env

say "apt packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip i2c-tools alsa-utils network-manager bluez libportaudio2 libatlas-base-dev ffmpeg \
  || sudo apt-get install -y -qq python3-venv python3-pip i2c-tools alsa-utils network-manager bluez libportaudio2 ffmpeg

say "enable I2C + groups"
if command -v raspi-config >/dev/null; then sudo raspi-config nonint do_i2c 0; fi
grep -q '^dtparam=i2c_arm=on' /boot/firmware/config.txt 2>/dev/null || grep -q '^dtparam=i2c_arm=on' /boot/config.txt 2>/dev/null \
  || echo 'dtparam=i2c_arm=on' | sudo tee -a "$( [[ -f /boot/firmware/config.txt ]] && echo /boot/firmware/config.txt || echo /boot/config.txt )" >/dev/null
sudo modprobe i2c-dev 2>/dev/null || true
grep -q '^i2c-dev' /etc/modules || echo i2c-dev | sudo tee -a /etc/modules >/dev/null
sudo usermod -aG i2c,audio,video,bluetooth,netdev "$ME" || true

say "python venv + deps (first run takes a few minutes)"
[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install -q -U pip
.venv/bin/pip install -q -r requirements-pi.txt
.venv/bin/pip install -q piper-tts 2>/dev/null || echo "(piper-tts wheel unavailable -> gTTS fallback will be used)"

say "Piper voice (offline TTS for the Nest)"
mkdir -p models
if [[ ! -f models/en_US-lessac-low.onnx ]]; then
  base=https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/low
  curl -fsSL -o models/en_US-lessac-low.onnx "$base/en_US-lessac-low.onnx" && curl -fsSL -o models/en_US-lessac-low.onnx.json "$base/en_US-lessac-low.onnx.json" \
    || echo "(voice download failed -> gTTS fallback)"
fi

say "detect Grove LCD on I2C (expect 0x3e text + 0x60/0x62 backlight)"
if i2cdetect -y 1 >/tmp/i2c.txt 2>&1; then
  cat /tmp/i2c.txt
  if grep -q ' 3e' /tmp/i2c.txt; then echo "LCD text controller found at 0x3E"; else echo "!! LCD 0x3E not seen — check the Grove cable is in an I2C port on the Base Hat, then re-run"; fi
else echo "i2cdetect failed (reboot may be required after enabling I2C)"; fi

say "direct-Ethernet link to the laptop (static 192.168.77.2, never default route)"
if command -v nmcli >/dev/null && nmcli -t -f DEVICE,TYPE dev | grep -q '^eth0:ethernet'; then
  nmcli -t -f NAME con show | grep -qx skyguard-direct || sudo nmcli con add type ethernet ifname eth0 con-name skyguard-direct \
    ipv4.method manual ipv4.addresses 192.168.77.2/24 ipv4.never-default yes ipv6.method ignore connection.autoconnect yes connection.autoconnect-priority -5 >/dev/null
fi

say "discover the Nest Mini (Chromecast) on the LAN"
set -a; source .env; set +a
.venv/bin/python scripts/nest_discover.py --write-env || echo "(Nest not found yet — join the Pi + Nest to the same hotspot, then re-run)"

say "systemd service"
sed "s|__USER__|$ME|g; s|__APP__|$APP|g" deploy/skyguard-hub.service | sudo tee /etc/systemd/system/skyguard-hub.service >/dev/null
sudo systemctl daemon-reload && sudo systemctl enable --now skyguard-hub.service
sleep 4; systemctl is-active skyguard-hub.service || { journalctl -u skyguard-hub -n 30 --no-pager; exit 1; }

IP=$(hostname -I | awk '{print $1}')
say "DONE. Dashboard: http://$IP:8000   (UNO Q: set HUB_URL=http://$IP:8000 in .env then run ./deploy/unoq.sh)"
