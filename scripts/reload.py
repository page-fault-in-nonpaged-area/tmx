#!/usr/bin/env python3
"""Apply generated appearance settings to the private server without restarting panes."""
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def reload(socket):
    if socket != 'tmx' and not socket.startswith('tmx-test-'):
        raise ValueError('unsupported TMX_SOCKET')
    if not socket or not all(c.isalnum() and c.isascii() or c in '-_' for c in socket):
        raise ValueError('invalid socket name')
    def mux(*args):
        return subprocess.check_output(['tmux', '-u', '-L', socket, *args], text=True, stderr=subprocess.PIPE, timeout=5).strip()
    if mux('show-option', '-gv', '@tmx') != 'on':
        raise ValueError('refusing to reload an unrelated server')
    # Apply only styling and the plugin attach hook, never layout or key bindings.
    mux('source-file', str(ROOT / '.generated/appearance.conf'))
    # Replace this repository's helpers, preserving the hourly weather cache.
    paths = {str(ROOT / 'status.py'), str(ROOT / 'plugins/status.py'), str(ROOT / 'plugins/ssh.py')}
    for directory in Path('/proc').iterdir():
        if not directory.name.isdigit():
            continue
        try:
            if directory.stat().st_uid != os.getuid():
                continue
            args = (directory / 'cmdline').read_bytes().split(b'\0')
            if len(args) != 5 or os.fsdecode(args[1]) not in paths or args[2:4] != [b'--watch', socket.encode()]:
                continue
            os.kill(int(directory.name), signal.SIGTERM)
            deadline = time.monotonic() + 5
            while directory.exists() and (directory / 'cmdline').read_bytes().strip(b'\0') and time.monotonic() < deadline:
                time.sleep(0.02)
            # The worker's flock is automatically released on exit.
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    env = {**os.environ, 'TMX_SOCKET': socket}
    # Update old cached workspace labels to reference the compiled colors.
    subprocess.run(['sh', str(ROOT / 'tmx.sh'), '--detached'], env=env, check=True, timeout=10,
                   stdout=subprocess.DEVNULL)
    settings = json.loads((ROOT / '.generated/plugins.json').read_text())
    if settings['plugins']['disk'] or settings['plugins']['weather']:
        command = 'exec python3 ' + shlex.quote(str(ROOT / 'plugins/status.py')) + ' --watch ' + shlex.quote(socket) + ' 2>/dev/null'
        mux('run-shell', '-b', command)
    command = 'exec python3 ' + shlex.quote(str(ROOT / 'plugins/ssh.py')) + ' --watch ' + shlex.quote(socket) + ' 2>/dev/null'
    mux('run-shell', '-b', command)


if __name__ == '__main__':
    try:
        reload(os.environ.get('TMX_SOCKET', 'tmx'))
        print('Reloaded tmx appearance and plugins; workspace programs remain running.')
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        sys.exit(f'tmx reload: {error}')
