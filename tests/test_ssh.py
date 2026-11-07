"""SSH parsing and real tmux rendering, using only a local stalled handshake."""
import os
from pathlib import Path
import re
import select
import shlex
import socket
import subprocess
import sys
import unittest
import uuid

import test_tmx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plugins import ssh


class DestinationTests(unittest.TestCase):
    def test_destinations_and_options(self):
        cases = [
            (['ssh', 'dev@example.com'], 'dev@example.com'),
            (['ssh', 'production'], 'production'),
            (['ssh', '-vvAt', '-p2222', '-i', '/some key', '-l', 'dev', 'example.com'], 'dev@example.com'),
            (['ssh', '-J', 'jump@bastion', '-o', 'User=dev', 'target', 'echo', 'other@host'], 'dev@target'),
            (['ssh', '-oUser dev', '-L8080:localhost:80', 'target'], 'dev@target'),
            (['ssh', 'ssh://dev@example.com:2222'], 'dev@example.com'),
            (['ssh', 'ssh://dev@[::1]:2222'], 'dev@[::1]'),
            (['ssh', '--', 'dev@host'], 'dev@host'),
            (['ssh', 'host', '-p', '2222', '-l', 'dev'], 'dev@host'),
            (['ssh', 'explicit@host', '-l', 'other'], 'explicit@host'),
            (['ssh', '-l', 'other', 'explicit@host'], 'other@host'),
            (['ssh', 'host', 'echo', '-l', 'other'], 'host'),
            (['ssh', '-G', 'host'], ''),
            (['ssh', '-i'], ''),
            (['ssh', '-Q', 'cipher'], ''),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.assertEqual(ssh.destination(argv), expected)

    def test_destination_cannot_inject_styles_formats_or_terminal_controls(self):
        value = ssh.destination(['ssh', 'bad\x1b[31m@host#{E:option}#[fg=red]'])
        self.assertNotIn('#', value)
        self.assertNotIn('\x1b', value)
        self.assertLessEqual(len(ssh.destination(['ssh', 'a' * 1000])), 240)


class SshPaneTests(unittest.TestCase):
    def setUp(self):
        self.t = test_tmx.TmxTests('test_sh_bash_workspace_creation_and_reuse')
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        self.t.start()
        self.listener = socket.socket()
        self.addCleanup(self.listener.close)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]

    def pane(self, workspace):
        return self.t.field('@tmx_workspace_' + str(workspace))

    def start_ssh(self, pane, username='dev', background=False):
        # The local listener never sends an SSH banner: no authentication,
        # host-key changes, real remote connection, or credentials are involved.
        argv = ['ssh', '-F', '/dev/null', '-o', 'ConnectTimeout=60',
                '-o', 'BatchMode=yes', '-p', str(self.port), username + '@127.0.0.1']
        command = shlex.join(argv) + (' >/dev/null 2>&1 &' if background else '')
        self.t.mux('send-keys', '-t', pane, '-l', command)
        self.t.mux('send-keys', '-t', pane, 'Enter')
        if not background:
            self.t.until(lambda: self.t.field('pane_current_command', pane) == 'ssh')
        return argv

    def render(self, pane, option):
        value = self.t.mux('display-message', '-p', '-t', pane, '#{E:' + option + '}')
        return re.sub(r'#\[[^]]*\]', '', value)

    def test_foreground_detection_disconnect_and_background_negative_control(self):
        pane = self.pane(1)
        self.start_ssh(pane)
        connection, _ = self.listener.accept()
        self.addCleanup(connection.close)
        ssh.poll(self.t.socket)
        self.assertEqual(self.t.field('@tmx_ssh', pane), 'dev@127.0.0.1')
        self.assertIn('[ssh:dev@127.0.0.1]', self.render(pane, '@tmx_header_pane'))
        self.t.mux('send-keys', '-t', pane, 'C-c')
        self.t.until(lambda: self.t.field('pane_current_command', pane) == 'bash')
        # The native format hides stale labels even before the next poll.
        self.assertNotIn('[ssh:', self.render(pane, '@tmx_header_pane'))
        ssh.poll(self.t.socket)
        self.assertEqual(self.t.field('@tmx_ssh', pane), '')
        self.start_ssh(pane, background=True)
        self.t.until(lambda: self.listener in select.select([self.listener], [], [], 0)[0])
        self.t.until(lambda: self.t.field('pane_current_command', pane) == 'bash')
        ssh.poll(self.t.socket)
        self.assertEqual(self.t.field('@tmx_ssh', pane), '')

    def test_worker_updates_and_exits_with_session(self):
        env = {**self.t.env, 'TMX_STATUS_DISABLED': '0'}
        worker = subprocess.Popen([sys.executable, str(ROOT / 'plugins/ssh.py'),
                                   '--watch', self.t.socket], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        def stop():
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=5)
        self.addCleanup(stop)
        pane = self.pane(6)
        self.start_ssh(pane)
        self.t.until(lambda: self.t.field('@tmx_ssh', pane) == 'dev@127.0.0.1')
        self.t.mux('send-keys', '-t', pane, 'C-c')
        self.t.until(lambda: self.t.field('@tmx_ssh', pane) == '')
        self.t.mux('kill-session', '-t', '=tmx')
        self.assertEqual(worker.wait(timeout=5), 0)

    def test_rendered_headers_right_suppression_switches_and_vertical_footer(self):
        self.t.mux('set-option', '-g', '@tmx_storage', 'sda: 78%')
        self.t.mux('set-option', '-g', '@tmx_weather', 'WEATHER')
        outer = 'tmx-test-ssh-view-' + uuid.uuid4().hex
        def view(*args):
            return subprocess.check_output(['tmux', '-L', outer, *args], text=True, timeout=5)
        self.addCleanup(lambda: subprocess.run(['tmux', '-L', outer, 'kill-server'], capture_output=True))
        command = shlex.join(['/usr/bin/env', '-u', 'TMUX', '-u', 'TMUX_PANE',
                              'TERM=xterm-256color',
                              'TMX_SOCKET=' + self.t.socket, 'TMX_STATUS_DISABLED=1',
                              'XDG_STATE_HOME=' + self.t.env['XDG_STATE_HOME'], str(test_tmx.SCRIPT)])
        view('-f', '/dev/null', 'new-session', '-d', '-s', 'view', '-x', '180', '-y', '40', command)
        view('set-option', '-g', 'status', 'off')
        def screen(ansi=False):
            return view('capture-pane', '-p', '-N', *(['-e'] if ansi else []), '-t', '=view:').splitlines()
        self.t.until(lambda: 'WEATHER' in screen()[0])
        left, right = self.pane(1), self.pane(6)
        before = self.t.mux('list-panes', '-s', '-F', '#{pane_id} #{pane_pid}')
        self.start_ssh(left, 'left')
        ssh.poll(self.t.socket)
        self.t.until(lambda: '[ssh:left@127.0.0.1]' in screen()[0])
        self.assertIn('WEATHER', screen()[0])
        self.assertIn('sda: 78%', screen()[0])
        self.start_ssh(right, 'right')
        ssh.poll(self.t.socket)
        self.t.until(lambda: '[ssh:right@127.0.0.1]' in screen()[0] and 'WEATHER' not in screen()[0])
        row = screen()[0]
        self.assertNotIn('sda:', row)
        self.assertRegex(row, r'\d{2}/\d{2} \d{2}:\d{2}')
        self.assertEqual(row.index('┬'), int(self.t.field('pane_width', left)))
        self.assertIn('\x1b[30m\x1b[48;5;51m[ssh:', screen(True)[0])
        self.assertIn('[ssh:right@127.0.0.1]\x1b[37m\x1b[49m', screen(True)[0])
        # A parked SSH workspace must not suppress the visible right header.
        self.assertEqual(self.t.launch('workspace', '7').returncode, 0)
        self.t.until(lambda: 'WEATHER' in screen()[0] and '[ssh:right' not in screen()[0])
        self.assertEqual(self.t.launch('workspace', '6').returncode, 0)
        self.t.until(lambda: '[ssh:right' in screen()[0] and 'WEATHER' not in screen()[0])
        self.assertEqual(self.t.launch('layout').returncode, 0)
        self.t.until(lambda: 'WEATHER' in screen()[-1])
        for pane, username in ((left, 'left'), (right, 'right')):
            index = int(self.t.field('pane_top', pane)) - 1
            self.assertIn('[ssh:' + username + '@127.0.0.1]', screen()[index])
        self.assertIn('sda: 78%', screen()[-1])
        self.assertEqual(self.t.launch('layout').returncode, 0)
        self.t.until(lambda: '[ssh:right' in screen()[0])
        # A long destination is trimmed without moving the divider or tabs.
        self.t.mux('set-option', '-p', '-t', right, '@tmx_ssh', 'dev@' + 'a' * 150)
        view('resize-window', '-x', '80', '-y', '40')
        self.t.until(lambda: int(self.t.field('window_width', left)) == 80 and '…' in screen()[0])
        self.assertEqual(screen()[0].index('┬'), int(self.t.field('pane_width', left)))
        self.assertLessEqual(len(screen()[0]), 80)
        after = self.t.mux('list-panes', '-s', '-F', '#{pane_id} #{pane_pid}')
        self.assertEqual(sorted(before.splitlines()), sorted(after.splitlines()))
