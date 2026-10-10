# Keeps the App Lab app alive so the sketch stays flashed/running. All SkyGuard logic runs as the
# `skyguard-node-b` systemd service on the Linux host (it needs host Wi-Fi/BLE access, which the app container lacks).
import time
from arduino.app_utils import App


def loop():
    time.sleep(1)


App.run(user_loop=loop)
