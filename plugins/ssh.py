#!/usr/bin/env python3
"""Publish the foreground SSH destination for visible workspace panes."""
import fcntl
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit


def destination(argv):
    """Read command-line destinations only; never run SSH config/Match commands."""
    user = None
    host = None
    arguments = iter(argv[1:])
    takes_value = 'BbcDEeFIiJLlmOopPRSWw'
    switches = '46AaCfGgKkMNnqsTtVvXxYy'
    for argument in arguments:
        if argument == '--':
            if host is not None:
                break
            argument = next(arguments, '')
        if not argument.startswith('-') or argument == '-':
            if host is not None:
                break  # The remote command and its arguments are not SSH options.
            host = argument
            if host.startswith('ssh://'):
                try:
                    uri = urlsplit(host)
                    user = user if user is not None else unquote(uri.username or '') or None
                    host = uri.hostname or ''
                    if ':' in host:
                        host = '[' + host + ']'
                except ValueError:
                    return ''
            elif '@' in host:
                explicit_user, host = host.rsplit('@', 1)
                user = user if user is not None else explicit_user
            continue  # OpenSSH also accepts options immediately after the host.
        flags = argument[1:]
        while flags:
            flag, flags = flags[0], flags[1:]
            if flag in takes_value:
                value = flags or next(arguments, None)
                if value is None:
                    return ''
                if flag == 'l' and user is None:
                    user = value
                elif flag == 'o':
                    option = re.split(r'[=\s]+', value.strip(), maxsplit=1)
                    if len(option) == 2 and option[0].lower() == 'user' and user is None:
                        user = option[1]
                break
            if flag not in switches or flag in 'GV':
                return ''
    if not host:
        return ''
    label = (user + '@' if user else '') + host
    # No terminal escapes or tmux formats/styles can enter the rendered header.
    return re.sub(r'[^a-zA-Z0-9_.:@%+\[\]-]', '?', label)[:240]


def foreground_destination(tty):
    rows = subprocess.check_output(
        ['ps', '-t', tty[5:] if tty.startswith('/dev/') else tty, '-o', 'pid=,pgid=,tpgid=,comm='],
        text=True, stderr=subprocess.DEVNULL, timeout=2)
    for row in rows.splitlines():
        fields = row.split()
        if len(fields) != 4:
            continue
        pid, group, foreground, command = fields
        if group != foreground or int(foreground) <= 0 or command != 'ssh':
            continue
        try:
            process = Path('/proc') / pid
            if Path(os.readlink(process / 'exe')).name != 'ssh':
                continue
            argv = [os.fsdecode(arg) for arg in (process / 'cmdline').read_bytes().split(b'\0')[:-1]]
            return destination(argv)
        except (OSError, ValueError):
            continue  # A foreground process may exit between snapshots.
    return ''


def poll(socket):
    def mux(*args):
        return subprocess.check_output(['tmux', '-u', '-L', socket, *args], text=True,
                                       stderr=subprocess.DEVNULL, timeout=5)
    rows = mux('list-panes', '-s', '-t', '=tmx', '-F',
               '#{pane_id}\t#{pane_tty}\t#{pane_current_command}\t#{window_id}\t#{@tmx_main_window}\t#{@tmx_ssh}')
    for row in rows.splitlines():
        pane, tty, command, window, main, previous = row.split('\t', 5)
        if window != main:
            continue
        value = ''
        if command == 'ssh':
            try:
                value = foreground_destination(tty)
            except (OSError, subprocess.SubprocessError):
                pass
        if value != previous:
            try:
                mux('set-option', '-p', '-t', pane, '@tmx_ssh', value)
            except subprocess.CalledProcessError:
                pass  # A pane may disappear during a poll.


def watch(socket):
    if os.environ.get('TMX_STATUS_DISABLED') == '1':
        return
    if not re.fullmatch(r'tmx(?:-test-[a-zA-Z0-9_-]+)?', socket):
        return
    root = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'tmx'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / (socket + '.ssh.lock')).open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        while True:
            try:
                poll(socket)
            except (OSError, subprocess.SubprocessError):
                return  # The private session ended.
            time.sleep(1)


if __name__ == '__main__':
    os.umask(0o077)
    if len(sys.argv) == 3 and sys.argv[1] == '--watch':
        watch(sys.argv[2])
