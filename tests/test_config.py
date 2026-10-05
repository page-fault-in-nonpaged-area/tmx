"""Exercise generated settings with actual tmux, without live sockets or HTTP."""
import importlib.util
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('configure', ROOT / 'scripts/configure.py')
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.yml'
        self.output = self.root / 'generated'
        self.data = yaml.safe_load((ROOT / 'config.yml').read_text())

    def build(self):
        self.config.write_text(yaml.safe_dump(self.data))
        configure.configure(ROOT, self.config, self.output)

    def test_invalid_config_preserves_working_artifacts(self):
        self.build()
        before = {path.name: path.read_bytes() for path in self.output.iterdir()}
        bad = [lambda d: d['plugins'].update(weather='false'),
               lambda d: d['colors'].update(active_x="red]'; run-shell bad"),
               lambda d: d['weather'].update(latitude=float('nan')),
               lambda d: d['weather'].update(longitude=181),
               lambda d: d['plugins'].update(unknown=True),
               lambda d: d['colors'].update(text='colour256')]
        for mutate in bad:
            with self.subTest(mutate=mutate):
                self.data = yaml.safe_load((ROOT / 'config.yml').read_text())
                mutate(self.data)
                with self.assertRaises(ValueError):
                    self.build()
                self.assertEqual(before, {p.name: p.read_bytes() for p in self.output.iterdir()})
        self.config.write_text((ROOT / 'config.yml').read_text() + '\nplugins: {}\n')
        with self.assertRaises(ValueError):
            configure.configure(ROOT, self.config, self.output)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.output.iterdir()})

    def test_unchanged_config_does_not_rewrite_generated_files(self):
        self.build()
        before = {p.name: p.stat().st_mtime_ns for p in self.output.iterdir()}
        self.build()
        self.assertEqual(before, {p.name: p.stat().st_mtime_ns for p in self.output.iterdir()})
        settings = json.loads((self.output / 'plugins.json').read_text())
        self.assertEqual(settings['weather'], {'city': 'Toronto,Canada', 'latitude': 43.6532, 'longitude': -79.3832})

    @unittest.skipUnless(shutil.which('tmux'), 'tmux required')
    def test_all_plugin_combinations_and_missing_weather_render_cleanly(self):
        for switches in itertools.product((False, True), repeat=3):
            with self.subTest(switches=switches):
                self.data['plugins'] = dict(zip(('disk', 'weather', 'clock'), switches))
                self.build()
                socket = 'tmx-test-config-' + uuid.uuid4().hex
                def mux(*args):
                    return subprocess.check_output(['tmux', '-L', socket, *args], text=True, timeout=5).strip()
                try:
                    mux('-f', str(self.output / 'tmux.conf'), 'new-session', '-d', '-s', 'tmx', '-x', '80', '-y', '30')
                    mux('set-option', '-p', '-t', '=tmx:', '@tmx_side', 'right')
                    mux('set-option', '-g', '@tmx_header_tabs', '[●] [2] [3]')
                    mux('set-option', '-g', '@tmx_storage', 'sda: 78%' if switches[0] else '')
                    mux('set-option', '-g', '@tmx_weather', '☀️ 24° UV 5' if switches[1] else '')
                    def render(option='@tmx_header_block'):
                        result = mux('display-message', '-p', '-t', '=tmx:', '#{E:' + option + '}')
                        return re.sub(r'#\[[^]]*\]', '', result).strip()
                    parts = []
                    if switches[0]: parts.append('sda: 78%')
                    if switches[1]: parts.append('☀️ 24° UV 5')
                    if switches[2]: parts.append(r'\d{2}/\d{2} \d{2}:\d{2}')
                    expected = r'\[ ' + r' \| '.join(parts) + r' \]' if parts else ''
                    self.assertRegex(render(), '^' + expected + '$')
                    # The stacked footer uses the whole window, including widths
                    # above 99 columns where lexical comparisons are incorrect.
                    mux('resize-window', '-x', '100', '-y', '30')
                    self.assertRegex(render('@tmx_footer'), '^' + expected + '$')
                    mux('resize-window', '-x', '80', '-y', '30')
                    mux('set-option', '-g', '@tmx_weather', '')
                    parts = [p for p in parts if p != '☀️ 24° UV 5']
                    expected = r'\[ ' + r' \| '.join(parts) + r' \]' if parts else ''
                    self.assertRegex(render(), '^' + expected + '$')
                    self.assertRegex(render('@tmx_footer'), '^' + expected + '$')
                    if not switches[0] and not switches[1]:
                        self.assertNotIn('run-shell', mux('show-hooks', '-g', 'client-attached'))
                    mux('resize-window', '-x', '18', '-y', '30')
                    self.assertEqual(render(), '')
                    mux('resize-window', '-x', '12', '-y', '30')
                    self.assertEqual(render('@tmx_footer'), '')
                finally:
                    subprocess.run(['tmux', '-L', socket, 'kill-server'], capture_output=True)

    def test_custom_colors_reach_tmux_disk_weather_and_clock(self):
        self.data['colors'].update(text='brightwhite', active_x='#abcdef', active_border='cyan',
                                   clock_date='yellow', disk_high='colour42', weather_uv='magenta')
        self.data['colors']['weather']['SUNY'] = 'colour123'
        self.build()
        compiled = (self.output / 'tmux.conf').read_text()
        self.assertIn("@tmx_color_active_x '#abcdef'", compiled)
        self.assertIn('pane_active,cyan,colour244', compiled)
        self.assertIn('#[fg=yellow]%m/%d', compiled)
        from plugins import disk, weather
        colors = json.loads((self.output / 'plugins.json').read_text())['colors']
        with patch.dict(disk.COLORS, colors), patch.object(disk.os, 'statvfs') as stat:
            stat.return_value.f_blocks, stat.return_value.f_bavail = 100, 78
            self.assertEqual(disk.storage(), '#[fg=brightwhite]sda: #[fg=colour42]78%#[fg=brightwhite]')
        with patch.dict(weather.THEME, colors), patch.dict(weather.COLORS, colors['weather']):
            self.assertEqual(weather.render(weather.reading('SUNY', 24, 5)),
                             '#[fg=colour123]☀️#[fg=brightwhite] 24° #[fg=magenta]UV#[fg=brightwhite] 5')

    def test_disabled_plugins_do_not_start_worker_or_call_providers(self):
        from plugins import status
        with patch.dict(status.ENABLED, disk=False, weather=False), \
             patch.dict(os.environ, {}, clear=True), patch.object(status.subprocess, 'run') as run:
            status.watch('tmx-test-disabled')
            run.assert_not_called()
        with patch.dict(status.ENABLED, disk=False, weather=True), \
             patch.dict(os.environ, {'XDG_STATE_HOME': str(self.root)}, clear=True), \
             patch.object(status.subprocess, 'run') as run, \
             patch.object(status.disk, 'storage') as storage, \
             patch.object(status.weather, 'cycle') as cycle, \
             patch.object(status.time, 'sleep') as sleep:
            # Exactly one poll followed by session shutdown; no real tmux/HTTP.
            run.side_effect = [subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0),
                               subprocess.CompletedProcess([], 1)]
            status.watch('tmx-test-disabled')
            storage.assert_not_called()
            cycle.assert_called_once()
            sleep.assert_called_once_with(60)

        with patch.dict(status.ENABLED, disk=True, weather=False), \
             patch.dict(os.environ, {'XDG_STATE_HOME': str(self.root)}, clear=True), \
             patch.object(status.subprocess, 'run') as run, \
             patch.object(status.disk, 'storage', return_value='sda: 78%') as storage, \
             patch.object(status.weather, 'cycle') as cycle, \
             patch.object(status.time, 'sleep') as sleep:
            run.side_effect = [subprocess.CompletedProcess([], 0)] * 3 + [subprocess.CompletedProcess([], 1)]
            status.watch('tmx-test-disabled')
            storage.assert_called_once()
            cycle.assert_not_called()
            sleep.assert_called_once_with(60)
