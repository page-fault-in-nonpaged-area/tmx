import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('tmux') and shutil.which('flock'), 'tmux and flock required')
class ReloadTests(unittest.TestCase):
    def test_reload_changes_colors_and_stops_disabled_worker_without_restarting_panes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('tmx.sh', 'tmux.conf', 'config.yml'):
                shutil.copy2(ROOT / name, root / name)
            for name in ('plugins', 'scripts'):
                shutil.copytree(ROOT / name, root / name, ignore=shutil.ignore_patterns('__pycache__'))
            data = yaml.safe_load((root / 'config.yml').read_text())
            data['plugins']['weather'] = False
            (root / 'config.yml').write_text(yaml.safe_dump(data))
            socket = 'tmx-test-reload-' + uuid.uuid4().hex
            env = {**os.environ, 'TMX_SOCKET': socket, 'XDG_STATE_HOME': str(root / 'state'), 'TERM': 'xterm-256color'}
            for name in ('TMUX', 'TMUX_PANE', 'TMX_STATUS_DISABLED', 'TMX_WEATHER_DISABLED'):
                env.pop(name, None)
            def run(*args):
                return subprocess.check_output(args, env=env, text=True, cwd=root, stderr=subprocess.PIPE, timeout=10).strip()
            def mux(*args): return run('tmux', '-L', socket, *args)
            worker = None
            try:
                run('python3', 'scripts/configure.py')
                run('sh', 'tmx.sh', '--detached')
                run('sh', 'tmx.sh', '--detached', '2')
                run('sh', 'tmx.sh', 'layout')
                self.assertEqual(mux('show-option', '-qv', '-t', '=tmx:', 'status-position'), 'bottom')
                snapshot = mux('list-panes', '-a', '-F', '#{pane_id} #{pane_pid} #{@tmx_workspace} #{pane_dead} #{window_id} #{pane_active}')
                worker = subprocess.Popen(['python3', str(root / 'plugins/status.py'), '--watch', socket], env=env)
                deadline = time.monotonic() + 5
                while not mux('show-option', '-gqv', '@tmx_storage') and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertIn('sda:', mux('show-option', '-gqv', '@tmx_storage'))
                data['colors'].update(active_x='blue', text='brightwhite')
                data['plugins'].update(disk=False, weather=False, clock=True)
                (root / 'config.yml').write_text(yaml.safe_dump(data))
                run('python3', 'scripts/configure.py')
                run('python3', 'scripts/reload.py')
                worker.wait(timeout=5)
                self.assertEqual(mux('list-panes', '-a', '-F', '#{pane_id} #{pane_pid} #{@tmx_workspace} #{pane_dead} #{window_id} #{pane_active}'), snapshot)
                self.assertEqual(mux('show-option', '-qv', '-t', '=tmx:', 'status-position'), 'bottom')
                self.assertEqual(mux('show-option', '-wqv', '-t', '=tmx:1', 'pane-border-status'), 'top')
                self.assertEqual(mux('show-option', '-qv', '-t', '=tmx:', '@tmx_vertical'), '1')
                self.assertEqual(mux('show-option', '-gqv', '@tmx_color_active_x'), 'blue')
                self.assertEqual(mux('show-option', '-gqv', '@tmx_color_text'), 'brightwhite')
                self.assertEqual(mux('show-option', '-gqv', '@tmx_storage'), '')
                self.assertEqual(mux('show-option', '-gqv', '@tmx_weather'), '')
                self.assertNotIn('run-shell', mux('show-hooks', '-g', 'client-attached'))
                # Resume disk-only operation with exactly one helper and no HTTP.
                data['plugins']['disk'] = True
                (root / 'config.yml').write_text(yaml.safe_dump(data))
                run('python3', 'scripts/configure.py')
                run('python3', 'scripts/reload.py')
                deadline = time.monotonic() + 5
                while not mux('show-option', '-gqv', '@tmx_storage') and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertIn('#[fg=brightwhite]sda:', mux('show-option', '-gqv', '@tmx_storage'))
                self.assertEqual(mux('list-panes', '-a', '-F', '#{pane_id} #{pane_pid} #{@tmx_workspace} #{pane_dead} #{window_id} #{pane_active}'), snapshot)
            finally:
                subprocess.run(['tmux', '-L', socket, 'kill-server'], capture_output=True)
                if worker is not None and worker.poll() is None:
                    worker.terminate()
                    worker.wait(timeout=5)
                # Clean up the detached disk-only test helper immediately.
                for directory in Path('/proc').iterdir():
                    try:
                        args = (directory / 'cmdline').read_bytes().split(b'\0')
                        if len(args) == 5 and args[1:4] == [str(root / 'plugins/status.py').encode(), b'--watch', socket.encode()]:
                            os.kill(int(directory.name), 15)
                    except (FileNotFoundError, ProcessLookupError, PermissionError, NotADirectoryError):
                        pass
