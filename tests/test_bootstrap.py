"""Exercise the piped installer against a local Git remote, without network."""
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import tempfile
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'https://github.com/page-fault-in-nonpaged-area/tmx.git'


class BootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remote_temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.remote_temp.cleanup)
        cls.remote = Path(cls.remote_temp.name) / 'remote'
        cls.remote.mkdir()
        for name in ('Makefile', 'config.yml', 'tmux.conf', 'tmx.sh'):
            shutil.copy2(ROOT / name, cls.remote / name)
        for name in ('scripts', 'plugins'):
            shutil.copytree(ROOT / name, cls.remote / name,
                            ignore=shutil.ignore_patterns('__pycache__'))
        for args in (['init', '-b', 'main'], ['add', '.'],
                     ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                      '-c', 'commit.gpgsign=false', 'commit', '-m', 'Fixture']):
            subprocess.run(['git', '-C', str(cls.remote), *args], check=True,
                           capture_output=True)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.env = {**os.environ, 'HOME': str(self.home),
                    'XDG_DATA_HOME': str(self.home / 'data'),
                    'BIN_DIR': str(self.home / 'bin'),
                    'GIT_CONFIG_COUNT': '1',
                    'GIT_CONFIG_KEY_0': f'url.{self.remote.as_uri()}.insteadOf',
                    'GIT_CONFIG_VALUE_0': REPOSITORY}
        self.env.pop('TMX_INSTALL_DIR', None)
        self.install_dir = self.home / 'data/tmx'

    def run_installer(self):
        return subprocess.run(['sh'], input=(ROOT / 'install.sh').read_text(),
                              text=True, capture_output=True, env=self.env,
                              cwd='/', timeout=30)

    def test_fresh_install_and_repeat_preserve_configuration(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.home / 'bin/tmx'
        self.assertEqual(target.resolve(), self.install_dir / 'tmx.sh')
        self.assertTrue((self.install_dir / '.generated/tmux.conf').is_file())
        config = self.install_dir / 'config.yml'
        config.write_text(config.read_text() + '\n# user customization\n')
        before = config.read_bytes()
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config.read_bytes(), before)
        self.assertIn('no git update', result.stdout)

    def test_existing_checkout_outside_default_location_is_reused(self):
        custom = self.home / 'custom checkout'
        self.env['TMX_INSTALL_DIR'] = str(custom)
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        del self.env['TMX_INSTALL_DIR']
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / 'bin/tmx').resolve(), custom / 'tmx.sh')
        self.assertFalse(self.install_dir.exists())

    def test_unrelated_command_is_preserved_before_download(self):
        target = self.home / 'bin/tmx'
        target.parent.mkdir()
        target.write_text('existing command')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing to replace', result.stderr)
        self.assertEqual(target.read_text(), 'existing command')
        self.assertFalse(self.install_dir.exists())

    def test_unrelated_directory_is_preserved(self):
        self.install_dir.mkdir(parents=True)
        sentinel = self.install_dir / 'keep'
        sentinel.write_text('user data')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unrelated directory', result.stderr)
        self.assertEqual(sentinel.read_text(), 'user data')
        self.assertFalse((self.home / 'bin/tmx').exists())

    def test_old_tmux_fails_before_download(self):
        shim_dir = self.home / 'shim'
        shim_dir.mkdir()
        shim = shim_dir / 'tmux'
        shim.write_text('#!/bin/sh\nprintf "tmux 3.2\\n"\n')
        shim.chmod(0o700)
        self.env['PATH'] = str(shim_dir) + os.pathsep + os.environ['PATH']
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('3.3 or newer', result.stderr)
        self.assertFalse(self.install_dir.exists())

    def test_alpine_next_version_label_is_accepted(self):
        shim_dir = self.home / 'shim'
        shim_dir.mkdir()
        shim = shim_dir / 'tmux'
        shim.write_text('#!/bin/sh\nif [ "$1" = -V ]; then printf "tmux next-3.4\\n"; '
                        'else exec ' + shlex.quote(shutil.which('tmux')) + ' "$@"; fi\n')
        shim.chmod(0o700)
        self.env['PATH'] = str(shim_dir) + os.pathsep + os.environ['PATH']
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / 'bin/tmx').is_symlink())

    def test_busybox_tools_report_required_alpine_packages_before_download(self):
        for command, package in (('readlink', 'coreutils'), ('ps', 'procps')):
            with self.subTest(command=command):
                shim_dir = self.home / command
                shim_dir.mkdir()
                shim = shim_dir / command
                shim.write_text('#!/bin/sh\nexit 1\n')
                shim.chmod(0o700)
                self.env['PATH'] = str(shim_dir) + os.pathsep + os.environ['PATH']
                result = self.run_installer()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('install ' + package, result.stderr)
                self.assertFalse(self.install_dir.exists())

    def test_installed_launcher_starts_and_toggles_without_locale_variables(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        socket = 'tmx-test-bootstrap-' + uuid.uuid4().hex
        self.addCleanup(lambda: subprocess.run(['tmux', '-L', socket, 'kill-server'],
                                               capture_output=True))
        env = {**self.env, 'TMX_SOCKET': socket, 'TMX_STATUS_DISABLED': '1',
               'XDG_STATE_HOME': str(self.home / 'state')}
        for name in ('LANG', 'LC_ALL', 'LC_CTYPE', 'TMUX', 'TMUX_PANE'):
            env.pop(name, None)
        target = str(self.home / 'bin/tmx')
        for arguments in (['--detached'], ['layout'], ['layout'], ['--detached']):
            result = subprocess.run([target, *arguments], env=env, cwd='/',
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        panes = subprocess.check_output(['tmux', '-u', '-L', socket, 'list-panes',
                                         '-s', '-F', '#{pane_id} #{@tmx_workspace}'],
                                        text=True)
        self.assertEqual(len(panes.splitlines()), 6)
        self.assertEqual({line.split()[1] for line in panes.splitlines()},
                         {'1', '2', '3', '6', '7', '8'})
