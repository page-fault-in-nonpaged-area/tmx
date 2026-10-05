import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import status


class StorageTests(unittest.TestCase):
    def test_available_percent_excludes_root_reserved_blocks(self):
        with patch.object(status.os, "statvfs", return_value=SimpleNamespace(
                f_blocks=1000, f_bfree=900, f_bavail=780)) as stat:
            self.assertEqual(status.storage(), "#[fg=white]sda: #[fg=green]78%#[fg=white]")
            stat.assert_called_once_with("/")

    def test_full_filesystem_and_storage_errors(self):
        for available in (0, -5):
            with patch.object(status.os, "statvfs", return_value=SimpleNamespace(
                    f_blocks=1000, f_bavail=available)):
                self.assertEqual(status.storage(), "#[fg=white]sda: #[fg=colour204]0%#[fg=white]")
        with patch.object(status.os, "statvfs", side_effect=OSError):
            self.assertEqual(status.storage(), "")
        with patch.object(status.os, "statvfs", return_value=SimpleNamespace(f_blocks=0)):
            self.assertEqual(status.storage(), "")
