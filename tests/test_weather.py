import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plugins import weather


class WeatherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / "cache.json"
        self.config = {"city": "Toronto,Canada", "latitude": 43.6532, "longitude": -79.3832}
        self.data = weather.reading("SUNY", 24, 5)
        self.config_patch = patch.object(weather, "load_config", return_value=self.config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    def test_emoji_format_preserves_numbers_and_uv_colors(self):
        self.assertEqual(weather.render(self.data),
                         "#[fg=colour220]☀️#[fg=white] 24° "
                         "#[fg=colour141]UV#[fg=white] 5")
        for state in weather.COLORS:
            self.assertRegex(state, r"^[A-Z]{4}$")
            self.assertIn(weather.ICONS[state], weather.render(weather.reading(state, 24, 5)))
        self.assertIn("colour39]🌧️", weather.render(weather.reading("RAIN", -2, 0)))
        self.assertEqual(weather.render(None), "")
        for value in (None, "NaN", "#[fg=red]", True, float("inf"), 1000):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                weather.reading("SUNY", value, 5)

    def test_existing_cached_reading_renders_emoji_without_provider_request(self):
        weather.save(self.cache, self.config, self.data, 100)
        before = self.cache.read_bytes()
        with patch.object(weather, "fetch_weather") as fetch:
            output = []
            weather.cycle(self.cache, output.append, now=101)
            fetch.assert_not_called()
        self.assertEqual(self.cache.read_bytes(), before)
        self.assertEqual(output, [weather.render(self.data)])
        self.assertIn("☀️", output[0])
        self.assertNotIn("SUNY", output[0])

    def test_wttr_parses_only_valid_data(self):
        record = {"current_condition": [{"weatherCode": "113", "temp_C": "24",
                   "uvIndex": "5", "weatherDesc": [{"value": "Sunny"}]}]}
        with patch.object(weather, "fetch_json", return_value=record):
            self.assertEqual(weather.wttr(self.config), self.data)
            record["current_condition"][0]["weatherDesc"][0]["value"] = "Clear "
            self.assertEqual(weather.wttr(self.config)["state"], "CLER")
            record["current_condition"][0]["uvIndex"] = None
            with self.assertRaises(TypeError):
                weather.wttr(self.config)

    def test_independent_fallback_and_all_errors_hide(self):
        with patch.object(weather, "wttr", side_effect=TimeoutError), \
             patch.object(weather, "open_meteo", return_value=self.data) as backup:
            self.assertEqual(weather.fetch_weather(self.config), self.data)
            backup.assert_called_once_with(self.config)
        with patch.object(weather, "wttr", side_effect=ValueError), \
             patch.object(weather, "open_meteo", side_effect=KeyError):
            self.assertEqual(weather.render(weather.fetch_weather(self.config)), "")

    def test_fallback_uses_current_hour_uv_not_daily_peak(self):
        data = {"current": {"time": 1000, "temperature_2m": -2.6, "weather_code": 95,
                            "is_day": 1}, "hourly": {"time": [0, 3600], "uv_index": [2, 9]}}
        with patch.object(weather, "fetch_json", return_value=data), \
             patch.object(weather.time, "time", return_value=1000):
            self.assertEqual(weather.open_meteo(self.config),
                             {"state": "STRM", "temperature": -3, "uv": 2})
            data["hourly"]["time"] = [10000, 13600]
            with self.assertRaises(ValueError):
                weather.open_meteo(self.config)

    def test_hourly_cache_failure_clears_and_does_not_retry_each_redraw(self):
        output = []
        with patch.object(weather, "fetch_weather", return_value=self.data) as fetch:
            weather.cycle(self.cache, output.append, now=100)
            self.assertEqual(output, ["", weather.render(self.data)])
            for now in (101, 159, 3699):
                weather.cycle(self.cache, output.append, now=now)
            self.assertEqual(fetch.call_count, 1)
            fetch.return_value = None
            output.clear()
            weather.cycle(self.cache, output.append, now=3700)
            self.assertEqual(output, ["", ""])
            weather.cycle(self.cache, output.append, now=4000)
            self.assertEqual(fetch.call_count, 2)
            self.assertIsNone(json.loads(self.cache.read_text())["reading"])
            fetch.return_value = self.data
            weather.cycle(self.cache, output.append, now=7300)
            self.assertEqual(fetch.call_count, 3)
            self.assertEqual(output[-1], weather.render(self.data))

    def test_corrupt_cache_and_location_changes_refresh(self):
        self.cache.write_text("broken")
        with patch.object(weather, "fetch_weather", return_value=self.data) as fetch:
            weather.cycle(self.cache, lambda value: None, now=100)
            self.config["city"] = "Ottawa,Canada"
            weather.cycle(self.cache, lambda value: None, now=101)
            self.assertEqual(fetch.call_count, 2)
        with patch.object(weather, "load_config", side_effect=ValueError):
            output = []
            weather.cycle(self.cache, output.append, now=102)
            self.assertEqual(output, [""])
