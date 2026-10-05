#!/usr/bin/env python3
"""Optional tmx weather: hourly HTTPS requests, native tmux status output.

No packages or API keys needed. All remote values are validated and rendered
locally; provider text is never interpreted as a tmux format or shell command.
"""
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

INTERVAL = 3600
CONFIG = Path(__file__).with_name("weather.json")
COLORS = {"SUNY": 220, "CLER": 159, "PART": 180, "CLDY": 250,
          "OVRC": 250, "MIST": 245, "FOGG": 245, "DRZL": 39,
          "RAIN": 39, "SNOW": 159, "SLET": 51, "STRM": 201}
WTTR = {
    "SUNY": [113], "PART": [116], "CLDY": [119], "OVRC": [122],
    "MIST": [143], "FOGG": [248, 260], "DRZL": [263, 266],
    "RAIN": [176, 293, 296, 299, 302, 305, 308, 353, 356, 359],
    "SNOW": [179, 227, 230, 323, 326, 329, 332, 335, 338, 368, 371],
    "SLET": [182, 185, 281, 284, 311, 314, 317, 320, 350, 362, 365, 374, 377],
    "STRM": [200, 386, 389, 392, 395],
}
WMO = {"SUNY": [0, 1], "PART": [2], "OVRC": [3], "FOGG": [45, 48],
       "DRZL": [51, 53, 55], "SLET": [56, 57, 66, 67],
       "RAIN": [61, 63, 65, 80, 81, 82], "SNOW": [71, 73, 75, 77, 85, 86],
       "STRM": [95, 96, 97, 99]}


def number(value, low, high):
    if isinstance(value, bool):
        raise ValueError("boolean instead of number")
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError("invalid weather number")
    return value


def state(code, table):
    code = number(code, 0, 999)
    return next(label for label, codes in table.items() if code in codes)


def reading(label, temperature, uv):
    if label not in COLORS:
        raise ValueError("unknown condition")
    return {"state": label, "temperature": round(number(temperature, -100, 65)),
            "uv": round(number(uv, 0, 30))}


def render(data):
    if data is None:
        return ""
    data = reading(data["state"], data["temperature"], data["uv"])
    return (f'#[fg=colour{COLORS[data["state"]]}]{data["state"]}'
            f'#[fg=white] {data["temperature"]}° #[fg=colour141]UV'
            f'#[fg=white] {data["uv"]}')


def fetch_json(url):
    request = Request(url, headers={"User-Agent": "tmx-weather/1.0", "Accept": "application/json"})
    with urlopen(request, timeout=8) as response:
        raw = response.read(262145)
    if len(raw) > 262144:
        raise ValueError("oversized response")
    return json.loads(raw)


def wttr(config):
    current = fetch_json("https://wttr.in/" + quote(config["city"], safe="") +
                         "?format=j1")["current_condition"][0]
    label = state(current["weatherCode"], WTTR)
    if label == "SUNY" and current.get("weatherDesc", [{}])[0].get("value", "").strip().lower() == "clear":
        label = "CLER"
    return reading(label, current["temp_C"], current["uvIndex"])


def open_meteo(config):
    query = urlencode({"latitude": config["latitude"], "longitude": config["longitude"],
                       "current": "temperature_2m,weather_code,is_day", "hourly": "uv_index",
                       "forecast_days": 1, "timeformat": "unixtime", "timezone": "auto"})
    data = fetch_json("https://api.open-meteo.com/v1/forecast?" + query)
    current, hourly = data["current"], data["hourly"]
    when = number(current["time"], 0, 1e12)
    if abs(time.time() - when) > 7200:
        raise ValueError("stale forecast")
    index = min(range(len(hourly["time"])), key=lambda i: abs(hourly["time"][i] - when))
    if abs(hourly["time"][index] - when) > 3600:
        raise ValueError("missing current UV")
    label = state(current["weather_code"], WMO)
    if label == "SUNY" and current["is_day"] == 0:
        label = "CLER"
    return reading(label, current["temperature_2m"], hourly["uv_index"][index])


def fetch_weather(config):
    for provider in (wttr, open_meteo):
        try:
            return provider(config)
        except Exception:
            continue
    return None


def load_config():
    data = json.loads(CONFIG.read_text())
    city = data["city"]
    if not isinstance(city, str) or not city.strip() or len(city) > 200:
        raise ValueError("invalid city")
    return {"city": city, "latitude": number(data["latitude"], -90, 90),
            "longitude": number(data["longitude"], -180, 180)}


def cached(cache, config, now):
    try:
        data = json.loads(cache.read_text())
        age = now - data["checked_at"]
        if data["config"] == config and 0 <= age < INTERVAL:
            render(data["reading"])  # Validate even cached values.
            return True, data["reading"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return False, None


def save(cache, config, data, now):
    with tempfile.NamedTemporaryFile(mode="w", dir=cache.parent, delete=False) as file:
        try:
            json.dump({"config": config, "reading": data, "checked_at": now}, file)
            file.flush()
            os.replace(file.name, cache)
        finally:
            if os.path.exists(file.name):
                os.unlink(file.name)


def cycle(cache, publish, now=None):
    now = time.time() if now is None else now
    try:
        config = load_config()
        fresh, data = cached(cache, config, now)
        if not fresh:
            publish("")  # Hide expired data before starting potentially slow requests.
            data = fetch_weather(config)
            save(cache, config, data, now)
        publish(render(data))
    except Exception:
        publish("")



if __name__ == "__main__":
    os.umask(0o077)
    try:
        if sys.argv[1:] == ["--once"]:
            print(render(fetch_weather(load_config())))
    except Exception:
        pass  # This optional plugin must never print errors into the status bar.
