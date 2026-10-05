import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('install', ROOT / 'scripts/install.py')
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'

    def test_idempotent_symlink_runs_from_unrelated_directory(self):
        target = install.install(self.bin)
        before = target.lstat().st_ino
        self.assertEqual(install.install(self.bin), target)
        self.assertEqual(target.lstat().st_ino, before)
        result = subprocess.run(['tmx', '--help'], cwd='/',
                                env={**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH']},
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Ctrl-q', result.stdout)
        self.assertEqual(target.resolve(), ROOT / 'tmx.sh')

    def test_refuses_files_unrelated_and_broken_symlinks(self):
        self.bin.mkdir()
        target = self.bin / 'tmx'
        target.write_text('existing user command')
        with self.assertRaises(ValueError): install.install(self.bin)
        self.assertEqual(target.read_text(), 'existing user command')
        target.unlink()
        target.symlink_to(self.root / 'missing')
        with self.assertRaises(ValueError): install.install(self.bin)
        self.assertEqual(os.readlink(target), str(self.root / 'missing'))
