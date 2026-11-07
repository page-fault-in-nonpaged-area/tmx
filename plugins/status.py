#!/usr/bin/env python3
"""Publish modular weather and root free-space values for the tmx top bar."""
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time

# Support direct execution from any working directory.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plugins import disk, weather
from plugins.settings import ENABLED


def watch(socket):
    if os.environ.get("TMX_STATUS_DISABLED") == "1" or not (ENABLED["disk"] or ENABLED["weather"]):
        return
    if socket != "tmx" and not socket.startswith("tmx-test-"):
        return
    if not all(c.isalnum() or c in "-_" for c in socket):
        return
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "tmx"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Retain the existing lock so an older worker cannot run alongside this one.
    with (root / (socket + ".weather.lock")).open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        last = {}
        def publish(option, value):
            if last.get(option) != value:
                subprocess.run(["tmux", "-u", "-L", socket, "set-option", "-g", option, value],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                last[option] = value
        cache = root / (socket + ".weather.json")
        while subprocess.run(["tmux", "-u", "-L", socket, "has-session", "-t", "=tmx"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5).returncode == 0:
            publish("@tmx_storage", disk.storage() if ENABLED["disk"] else "")
            if ENABLED["weather"] and os.environ.get("TMX_WEATHER_DISABLED") != "1":
                weather.cycle(cache, lambda value: publish("@tmx_weather", value))
            else:
                publish("@tmx_weather", "")
            time.sleep(60)  # Local disk checks each minute; weather cache lasts an hour.


if __name__ == "__main__":
    os.umask(0o077)
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--watch":
            watch(sys.argv[2])
    except Exception:
        pass  # An optional status helper must not print into the user's terminal.
