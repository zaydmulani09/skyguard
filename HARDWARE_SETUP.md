# Hardware day — exact steps (≈ 30 min)

Do them in order. Anything marked **(first time only)** you can skip on a re-deploy.

## 0. Parts → where each plugs in

| Part | Plugs into | Notes |
|---|---|---|
| Grove **Base Hat** | Pi 40-pin GPIO header (press down fully) | Hat has Grove sockets; the one labelled **I2C** is bus 1 |
| Grove **LCD RGB Backlight v4.0** | Grove cable → Base Hat **I2C** socket | Text controller 0x3E, backlight 0x60/0x62 (auto-detected) |
| **Logitech webcam** (camera + mic) | Pi USB 3 (blue) port | One cable = video + mic |
| **BENFEI USB-Ethernet** | Laptop USB; Ethernet cable → Pi RJ-45 | Direct wired SSH, immune to venue Wi-Fi isolation |
| **Arduino UNO Q** | USB-C power only (5 V / 3 A) | Needs no screen after first-time setup |
| **Nest Mini** | Wall power | Same Wi-Fi as the Pi |
| Grove air-quality + light sensors | — | Unused |

## 1. One shared network = your phone hotspot (first time only)
1. Phone → hotspot on. **Name/password it exactly the same at home and at the venue** (e.g. `SkyGuardNet` / a password you choose). Devices remember it, so at the venue they just join.
2. Keep Wi-Fi band at 2.4 GHz if the phone offers a choice (Nest Mini + Pi are happiest there).

## 2. Nest Mini (first time only)
1. Plug in the Nest Mini. On a **second phone** (the hotspot phone can't join its own hotspot) open the **Google Home** app → **+ → Set up device → New device**.
2. When it asks for Wi-Fi pick `SkyGuardNet`. Name the speaker (default "Nest Mini" is fine; if you rename it, put the name in `.env` as `NEST_NAME`).
3. Done. Nothing else on the Nest — SkyGuard casts audio to it over the LAN.

## 3. Raspberry Pi 4 — flash (first time only)
1. **Raspberry Pi Imager** → *Raspberry Pi OS Lite (64-bit)* → ⚙ settings:
   hostname `skyguard-pi` · user `pi` + a password · **enable SSH** · Wi-Fi = `SkyGuardNet` + password + your country code.
2. Boot the Pi with Base Hat, LCD, webcam attached. Wait ~90 s. Find its IP: phone hotspot "connected devices" list (or `ping skyguard-pi.local`).

## 4. Laptop ↔ Pi wired link (survives venue Wi-Fi isolation)
The deploy script configures the Pi side (`192.168.77.2`). On the laptop (PowerShell **as admin**) set the adapter side once:
```powershell
Get-NetAdapter | ? InterfaceDescription -match 'USB|BENFEI|Realtek|Ethernet'   # note the Name, e.g. "Ethernet 3"
New-NetIPAddress -InterfaceAlias "Ethernet 3" -IPAddress 192.168.77.1 -PrefixLength 24
```
Plug the cable laptop-adapter → Pi. (Until step 5 has run once, use the Pi's hotspot IP for SSH instead.)

## 5. Deploy Node A (the one command)
```bash
cp .env.example .env     # edit: PI_HOST = Pi hotspot IP (later 192.168.77.2), PI_USER, NEST_NAME if renamed
./deploy/pi.sh
```
It syncs the repo, enables I2C, installs deps + Piper voice, **runs `i2cdetect` and tells you if the LCD (0x3E) is seen**, finds the Nest on the LAN and writes `NEST_NAME`, installs + starts the `skyguard-hub` systemd service. Then:
* Dashboard from the laptop: `http://192.168.77.2:8000` (wired) — or from any phone on the hotspot: `http://<pi hotspot ip>:8000`
* Logs: `ssh pi@192.168.77.2 journalctl -u skyguard-hub -f`
* LCD should now read `CLEAR / SkyGuard armed` on green. If it stays blank: `ssh pi@… i2cdetect -y 1`; no `3e` → re-seat the Grove cable / reboot once (I2C only activates after reboot the very first time).

## 6. UNO Q (Node B)
1. **(first time only)** Power the UNO Q over USB-C, open **Arduino App Lab** on the laptop, connect over USB, finish first-boot setup (set the `arduino` user password, join `SkyGuardNet`). Note its IP / hostname.
2. In `.env` set `UNOQ_HOST`, `UNOQ_USER=arduino`, and `HUB_URL=http://<Pi hotspot IP>:8000` (the address the UNO Q can reach the Pi on).
3. ```bash
   ./deploy/unoq.sh
   ```
   Installs deps, flashes the LED-matrix sketch through `arduino-app-cli`, installs the `skyguard-node-b` service. Matrix shows a slow sweep when clear; a flashing ⚠ on DRONE.
   If `arduino-app-cli` isn't found, import `unoq_app/` in App Lab and press Run once (that flashes the sketch), then re-run the script.

## 7. Check it end-to-end (2 min)
1. Open the dashboard → both nodes **online** in *Node health*.
2. Press **Test · Wi-Fi RF** (or **Run demo**): LCD → red flashing `DRONE nn% NE`, UNO Q matrix flashes ⚠, the Nest speaks ("Drone detected, north east, confidence …"). Press **Stop / reset**.
3. For a real RF test without a drone: phone hotspot named `TELLO-123ABC` for 30 s — the Wi-Fi detector flags it (turn it off after; your real hotspot name must be restored first so nothing else drops).
4. Walk in front of the camera / play drone audio from a laptop speaker near the webcam mic for the vision / audio paths.

## Troubleshooting (the likely ones)
| Symptom | Fix |
|---|---|
| `ssh` times out on `192.168.77.2` | Laptop adapter IP not set (step 4) or cable on the wrong Pi port; use hotspot IP meanwhile |
| Nest doesn't speak | Pi and Nest on different networks / hotspot client isolation: `ssh pi@… ~/skyguard/.venv/bin/python scripts/nest_discover.py` must list it. Laptop-speaker fallback speaks from the dashboard host anyway |
| No Wi-Fi networks listed | `nmcli dev wifi list` on the Pi; needs NetworkManager (default on Bookworm) |
| No BLE results | `sudo systemctl status bluetooth`; the Pi's Bluetooth is only a *scanner* here |
| UNO Q matrix dark | Sketch not flashed yet (see step 6), or `ls -l /var/run/arduino-router.sock` missing |
| Camera black | `ls /dev/video*`; unplug/replug; another process holding it (`sudo fuser /dev/video0`) |
| Mic silent | `arecord -l`, then set `ALSA_DEVICE=plughw:<card>,0` in `.env` and `sudo systemctl restart skyguard-hub` |

**Switching hardware ↔ simulation is one line:** `SKYGUARD_PROFILE=` in `.env` (`sim` | `laptop` | `pi` | `unoq`), or `profile =` in `config.toml`.
