import concurrent.futures
import fcntl
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
import uuid

SCRIPT = Path(__file__).resolve().parents[1] / "tmx.sh"


@unittest.skipUnless(shutil.which("tmux") and shutil.which("flock"), "tmux and flock required")
class TmxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tmx-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.socket = "tmx-test-" + uuid.uuid4().hex
        self.env = {**os.environ, "TMX_SOCKET": self.socket, "TERM": "xterm-256color",
                    "XDG_STATE_HOME": str(self.root / "state"), "TMX_WEATHER_DISABLED": "1", "TMX_STATUS_DISABLED": "1"}
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)
        self.addCleanup(lambda: subprocess.run(
            ["tmux", "-L", self.socket, "kill-server"], capture_output=True))

    def launch(self, *args, shell="sh"):
        return subprocess.run([shell, str(SCRIPT), *args], env=self.env, cwd=self.root,
                              capture_output=True, text=True, timeout=10)

    def mux(self, *args):
        return subprocess.check_output(["tmux", "-L", self.socket, *args],
                                       text=True, timeout=5).strip()

    def start(self, *args):
        result = self.launch("--detached", *args)
        self.assertEqual(result.returncode, 0, result.stderr)

    def until(self, condition):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.03)
        self.fail("condition did not become true within five seconds")

    def snapshot(self):
        return self.mux("list-panes", "-a", "-F",
                        "#{window_index} #{pane_id} #{pane_pid} #{pane_active} #{pane_dead}")

    def active(self):
        return self.mux("display-message", "-p", "-t", "=tmx:",
                        "#{@tmx_workspace}:#{pane_index}")

    def attach(self):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 80, 0, 0))
        process = subprocess.Popen([str(SCRIPT)], env=self.env, cwd=self.root,
                                   stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        self.addCleanup(lambda: self.close_fd(master))
        def stop():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        self.addCleanup(stop)
        self.until(lambda: self.mux("list-clients", "-F", "#{client_tty}") != "")
        # Drain initial render before issuing keys.
        while select.select([master], [], [], 0.05)[0]:
            os.read(master, 65536)
        return process, master

    @staticmethod
    def close_fd(fd):
        try:
            os.close(fd)
        except OSError:
            pass

    def test_sh_bash_workspace_creation_and_reuse(self):
        self.start("3")
        first = self.snapshot()
        self.assertEqual(len(first.splitlines()), 2)
        self.assertEqual(self.active(), "3:1")
        self.assertEqual(self.launch("--detached", shell="bash").returncode, 0)
        self.assertEqual(first, self.snapshot())
        self.start("2")
        self.assertEqual(len(self.snapshot().splitlines()), 3)
        self.assertEqual(self.field("@tmx_workspace", "=tmx:1.2"), "4")
        self.start("3")
        self.assertEqual(self.active(), "3:1")
        self.assertEqual(self.snapshot().splitlines()[:2], first.splitlines())

    def test_concurrent_first_launch_and_workspace_creation(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.launch("--detached", "2"), range(4)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.snapshot().splitlines()), 2)
        self.assertEqual(self.active(), "2:1")

    def test_abrupt_disconnect_preserves_processes_focus_and_shell_state(self):
        self.start("4")
        self.mux("select-pane", "-t", "=tmx:1.2")
        self.mux("send-keys", "-t", "=tmx:1.2", "TMX_SENTINEL=still_here", "Enter")
        before = self.snapshot()
        process, terminal = self.attach()
        self.close_fd(terminal)  # Simulate the client terminal disappearing.
        process.wait(timeout=5)
        self.assertEqual(before, self.snapshot())
        second, terminal2 = self.attach()
        self.assertEqual(self.active(), "4:2")
        self.assertEqual(before, self.snapshot())
        # Query a non-exported variable in the same live shell.
        marker = self.root / "sentinel"
        self.mux("send-keys", "-t", "=tmx:1.2",
                 f'printf "%s" "$TMX_SENTINEL" > {marker}', "Enter")
        self.until(marker.exists)
        self.assertEqual(marker.read_text(), "still_here")
        os.write(terminal2, b"\x02d")
        self.assertEqual(second.wait(timeout=5), 0)

    def test_workspace_and_pane_shortcuts_and_split_blocking(self):
        self.start()
        process, terminal = self.attach()
        os.write(terminal, b"\x022")
        self.until(lambda: self.active() == "2:1")
        self.assertEqual(len(self.snapshot().splitlines()), 3)
        right = self.field("pane_id", "=tmx:1.2")
        os.write(terminal, b"\x02o")
        self.until(lambda: self.active() == "4:2")
        os.write(terminal, b"\x024")
        self.until(lambda: self.active() == "4:2")
        self.assertEqual(self.field("pane_id", "=tmx:1.2"), right)
        self.assertEqual(self.field("@tmx_workspace"), "2")
        os.write(terminal, b"\x025")
        self.until(lambda: self.active() == "5:2")
        before = self.snapshot()
        for key in (b'%', b'"', b'c', b'!', b'x', b':'):
            os.write(terminal, b"\x02" + key)
            time.sleep(0.03)
        self.assertEqual(before, self.snapshot())
        keys = self.mux("list-keys", "-T", "prefix")
        for command in ("split-window", "new-window", "kill-pane", "break-pane", "command-prompt"):
            self.assertNotIn(command, keys)
        os.write(terminal, b"\x02d")
        self.assertEqual(process.wait(timeout=5), 0)

    def test_extended_ctrl_digit_and_shift_left_shortcut(self):
        self.start()
        process, terminal = self.attach()
        os.write(terminal, b"\x1b[51;5u")  # CSI-u Ctrl+3.
        self.until(lambda: self.active() == "3:1")
        # Read the translated shortcut directly from a pane's raw terminal.
        capture = self.root / "key"
        command = f"stty raw -echo; dd bs=1 count=6 of={capture} 2>/dev/null; stty sane"
        self.mux("send-keys", "-t", "=tmx:1.1", command, "Enter")
        self.until(lambda: self.mux("display-message", "-p", "-t", "=tmx:1.1",
                                    "#{pane_current_command}") == "dd")
        os.write(terminal, b"\x02q")
        self.until(lambda: capture.exists() and capture.stat().st_size == 6)
        self.assertEqual(capture.read_bytes(), b"\x1b[1;2D")
        os.write(terminal, b"\x02d")
        self.assertEqual(process.wait(timeout=5), 0)

    def test_every_ctrl_digit_protocol_switches_only_focused_side(self):
        self.start()
        process, terminal = self.attach()
        for side, position, switch, protocol in (
            ("left", 1, None, "csi-u"), ("right", 2, b"\x0f", "xterm"),
        ):
            if switch:
                os.write(terminal, switch)
                self.until(lambda: self.active().endswith(":2"))
            other_position = 2 if position == 1 else 1
            other = self.field("pane_id", f"=tmx:1.{other_position}")
            for number in range(1, 4) if side == "left" else range(4, 7):
                code = ord(str(number))
                sequence = f"\x1b[{code};5u" if protocol == "csi-u" else f"\x1b[27;5;{code}~"
                os.write(terminal, sequence.encode())
                self.until(lambda: self.active() == f"{number}:{position}")
                self.assertEqual(self.field("pane_id", f"=tmx:1.{other_position}"), other)
                self.assertEqual(self.field("window_panes"), "2")
                self.assertEqual(self.field("window_id"), "@0")
                while select.select([terminal], [], [], 0)[0]:
                    os.read(terminal, 65536)
        self.assertEqual(len(self.snapshot().splitlines()), 6)
        os.write(terminal, b"\x02d")
        self.assertEqual(process.wait(timeout=5), 0)

    def test_ctrl_p_cycles_existing_workspaces_independently_and_wraps(self):
        self.start()
        # Deliberately create sparse workspace numbers out of numeric order.
        for number, side in ((3, "left"), (2, "left"), (6, "right"), (5, "right")):
            result = self.launch("workspace", str(number), side)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.mux("select-pane", "-t", "=tmx:1.1")
        processes = self.mux("list-panes", "-s", "-F", "#{pane_id} #{pane_pid}")
        process, terminal = self.attach()
        for position, sequence in ((1, (3, 1, 2, 3, 1)), (2, (6, 4, 5, 6, 4))):
            other_position = 3 - position
            other = self.field("pane_id", f"=tmx:1.{other_position}")
            for number in sequence:
                os.write(terminal, b"\x10")
                self.until(lambda: self.active() == f"{number}:{position}")
                self.assertEqual(self.mux("list-clients", "-F", "#{client_key_table}"), "root")
                self.assertEqual(self.field("pane_id", f"=tmx:1.{other_position}"), other)
                self.assertEqual(self.field("window_panes"), "2")
                while select.select([terminal], [], [], 0)[0]:
                    os.read(terminal, 65536)
            os.write(terminal, b"\x0f")
            self.until(lambda: self.active().endswith(f":{other_position}"))
        self.assertEqual(self.active(), "1:1")
        after = self.mux("list-panes", "-s", "-F", "#{pane_id} #{pane_pid}")
        self.assertEqual(sorted(processes.splitlines()), sorted(after.splitlines()))
        os.write(terminal, b"\x02d")
        self.assertEqual(process.wait(timeout=5), 0)

    def test_workspace_ownership_survives_pane_index_changes(self):
        self.start()
        for side in ("left", "right"):
            for number in (("2", "3", "1") if side == "left" else ("5", "6", "4")):
                self.assertEqual(self.launch("workspace", number, side).returncode, 0)
        left = self.field("pane_id", "=tmx:1.1")
        right = self.field("pane_id", "=tmx:1.2")
        processes = self.mux("list-panes", "-a", "-F", "#{pane_id} #{pane_pid}")
        # Index 1 now means the right pane. Ownership must not depend on it.
        self.mux("set-option", "-w", "-t", "=tmx:1", "pane-base-index", "0")
        result = self.launch("workspace", "next", "left")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.field("@tmx_side", "=tmx:1.0"), "left")
        self.assertEqual(self.field("@tmx_workspace", "=tmx:1.0"), "2")
        self.assertEqual(self.field("pane_id", "=tmx:1.1"), right)
        self.assertEqual(self.field("@tmx_workspace", right), "4")
        self.assertEqual(sorted(processes.splitlines()), sorted(self.mux(
            "list-panes", "-a", "-F", "#{pane_id} #{pane_pid}").splitlines()))
        self.assertNotEqual(self.field("window_id", left), self.field("window_id", right))

    def test_ctrl_p_ignores_stale_tmux_command_context(self):
        self.start()
        self.start("2")
        parked_right = self.field("pane_id", "=tmx:1.2")
        self.assertEqual(self.launch("workspace", "5", "right").returncode, 0)
        self.mux("select-pane", "-t", "=tmx:1.1")
        right = self.field("pane_id", "=tmx:1.2")
        process, terminal = self.attach()
        # Force a stale tmux target while retaining the real Ctrl-p command.
        # A handler trusting this parked right-pane context jumps sides.
        binding = self.mux("list-keys", "-T", "root", "C-p")
        binding = binding.replace("run-shell ", "run-shell -t " + parked_right + " ")
        config = self.root / "stale-context.conf"
        config.write_text(binding + "\n")
        self.mux("source-file", str(config))
        os.write(terminal, b"\x10")
        self.until(lambda: self.active() == "1:1")
        self.assertEqual(self.field("pane_id", "=tmx:1.2"), right)
        os.write(terminal, b"\x02d")
        self.assertEqual(process.wait(timeout=5), 0)

    def test_model_two_migration_preserves_terminals_and_registers_fixed_ids(self):
        self.start()
        for number in ("2", "3", "5", "6"):
            self.start(number)
        # Reconstruct the previous overlapping labels without touching terminals.
        for number in (4, 5, 6):
            pane = self.mux("show-option", "-v", "-t", "=tmx:", f"@tmx_workspace_{number}")
            self.mux("set-option", "-p", "-t", pane, "@tmx_workspace", str(number - 3))
        self.mux("set-option", "-t", "=tmx:", "@tmx_workspace_model", "2")
        before = self.snapshot()
        self.start()
        self.assertEqual(self.snapshot(), before)
        for number in range(1, 7):
            pane = self.mux("show-option", "-v", "-t", "=tmx:", f"@tmx_workspace_{number}")
            self.assertEqual(self.field("@tmx_workspace", pane), str(number))
            self.assertEqual(self.field("@tmx_side", pane), "left" if number < 4 else "right")
        self.assertEqual(self.active(), "6:2")

    def test_ctrl_p_rapid_repeats_keep_both_sides_independent(self):
        self.start()
        for side in ("left", "right"):
            for number in (("2", "3", "1") if side == "left" else ("5", "6", "4")):
                self.assertEqual(self.launch("workspace", number, side).returncode, 0)
        self.mux("select-pane", "-t", "=tmx:1.1")
        process, terminal = self.attach()
        for position in (1, 2):
            other = self.field("pane_id", f"=tmx:1.{3 - position}")
            for expected in (3, 2, 1, 3, 2, 1):
                os.write(terminal, b"\x10\x10")
                self.until(lambda: self.active() == f"{expected + (3 if position == 2 else 0)}:{position}")
                self.assertEqual(self.field("pane_id", f"=tmx:1.{3 - position}"), other)
                self.assertEqual(self.field("@tmx_side", "=tmx:1.1"), "left")
                self.assertEqual(self.field("@tmx_side", "=tmx:1.2"), "right")
                while select.select([terminal], [], [], 0.01)[0]:
                    os.read(terminal, 65536)
            os.write(terminal, b"\x0f")
            self.until(lambda: self.active().endswith(f":{3 - position}"))
        # Interleave pane and workspace shortcuts in one terminal input burst.
        for expected in (2, 3, 1) * 3:
            os.write(terminal, b"\x10\x0f\x10\x0f")
            self.until(lambda: self.active() == f"{expected}:1" and
                       self.field("@tmx_workspace", "=tmx:1.2") == str(expected + 3))
            while select.select([terminal], [], [], 0.01)[0]:
                os.read(terminal, 65536)
        os.write(terminal, b"\x02d")
        self.assertEqual(process.wait(timeout=5), 0)

    def test_header_stays_over_its_side_after_workspace_swaps(self):
        self.start()
        for number in ("2", "3", "5", "6", "1"):
            self.start(number)
        self.launch("workspace", "4")
        self.mux("select-pane", "-t", "=tmx:1.1")
        self.mux("set-option", "-g", "@tmx_storage", "#[fg=white]sda: 78%")
        self.mux("set-option", "-g", "@tmx_weather",
                 "#[fg=colour220]SUNY#[fg=white] 24° #[fg=colour141]UV#[fg=white] 5")
        # An outer isolated tmux acts as a terminal emulator for actual redraws.
        outer = "tmx-test-header-" + uuid.uuid4().hex
        def view(*args):
            return subprocess.check_output(["tmux", "-L", outer, *args], text=True, timeout=5)
        self.addCleanup(lambda: subprocess.run(["tmux", "-L", outer, "kill-server"], capture_output=True))
        command = shlex.join(["/usr/bin/env", "-u", "TMUX", "-u", "TMUX_PANE",
                              "TMX_SOCKET=" + self.socket, "TMX_STATUS_DISABLED=1",
                              "XDG_STATE_HOME=" + self.env["XDG_STATE_HOME"], str(SCRIPT)])
        view("-f", "/dev/null", "new-session", "-d", "-s", "view", "-x", "130", "-y", "30", command)
        self.until(lambda: self.mux("list-clients", "-F", "#{client_tty}") != "")
        def row():
            return view("capture-pane", "-p", "-N", "-t", "=view:").splitlines()[0]
        self.until(lambda: "SUNY" in row())
        right = self.field("pane_id", "=tmx:1.2")
        left_width = int(self.field("pane_width"))
        for number in (2, 3, 1, 2):
            view("send-keys", "-t", "=view:", "C-p")
            self.until(lambda: self.active() == f"{number}:1")
            # Wait for the terminal redraw, then check physical column placement.
            left_tabs = " ".join("[x]" if n == number else f"[{n}]" for n in (1, 2, 3))
            self.until(lambda: left_tabs in row()[:left_width])
            header = row()
            self.assertEqual(header[left_width], "┬", header)
            self.assertTrue(header.startswith("── " + left_tabs), header)
            self.assertIn("[x] [2] [3]", header[left_width + 1:])
            self.assertIn("sda: 78% | SUNY 24° UV 5 |", header[left_width + 1:])
            self.assertNotIn("SUNY", header[:left_width])
            ansi = view("capture-pane", "-p", "-e", "-N", "-t", "=view:").splitlines()[0]
            self.assertIn("\x1b[31mx", ansi.split("┬")[0])
            self.assertNotIn("\x1b[31mx", ansi.split("┬")[1])
            self.assertEqual(self.field("pane_id", "=tmx:1.2"), right)
        left = self.field("pane_id")
        view("send-keys", "-t", "=view:", "C-o")
        self.until(lambda: self.active() == "4:2")
        for number in (5, 6, 4):
            view("send-keys", "-t", "=view:", "C-p")
            self.until(lambda: self.active() == f"{number}:2")
            right_tabs = " ".join("[x]" if n == number else f"[{n - 3}]" for n in (4, 5, 6))
            self.until(lambda: right_tabs in row()[left_width + 1:])
            header = row()
            self.assertEqual(header[left_width], "┬", header)
            self.assertIn(right_tabs, header[left_width + 1:])
            self.assertEqual(self.field("pane_id"), left)
            ansi = view("capture-pane", "-p", "-e", "-N", "-t", "=view:").splitlines()[0]
            self.assertNotIn("\x1b[31mx", ansi.split("┬")[0])
            self.assertIn("\x1b[31mx", ansi.split("┬")[1])

    def test_ctrl_p_single_workspace_and_scrollback_preservation(self):
        process, terminal = self.scroll_fixture()
        before = self.snapshot()
        # With one workspace, Ctrl-p is a no-op and the following digit is
        # ordinary application input, not a lingering workspace prefix.
        os.write(terminal, b"\x101")
        self.until(lambda: (self.root / "input").read_bytes() == b"1")
        self.assertEqual(self.snapshot(), before)
        os.write(terminal, b"\x022")
        self.until(lambda: self.active() == "2:1")
        os.write(terminal, b"\x021")
        self.until(lambda: self.active() == "1:1")
        other = self.field("pane_id", "=tmx:1.2")
        for mode in ("vi", "emacs"):
            self.mux("set-option", "-w", "-t", "=tmx:1", "mode-keys", mode)
            self.mux("copy-mode", "-t", "=tmx:1.1")
            self.mux("send-keys", "-t", "=tmx:1.1", "-X", "-N", "3", "scroll-up")
            self.assertEqual(self.field("scroll_position"), "3")
            os.write(terminal, b"\x10")
            self.until(lambda: self.active() == "2:1")
            os.write(terminal, b"\x10")
            self.until(lambda: self.active() == "1:1")
            self.assertEqual(self.field("pane_in_mode"), "1")
            self.assertEqual(self.field("scroll_position"), "3")
            self.assertEqual(self.field("pane_id", "=tmx:1.2"), other)
            self.mux("send-keys", "-t", "=tmx:1.1", "-X", "cancel")
        self.assertEqual((self.root / "input").read_bytes(), b"1")
        os.write(terminal, b"\x02d")
        self.assertEqual(process.wait(timeout=5), 0)

    def test_legacy_digits_and_control_aliases_are_not_workspace_shortcuts(self):
        process, terminal = self.scroll_fixture()
        original = self.snapshot()
        data = b"123456789\x00\x7f\x1b"
        os.write(terminal, data)
        self.until(lambda: (self.root / "input").read_bytes() == data)
        self.assertEqual(self.snapshot(), original)
        os.write(terminal, b"\x02d")
        self.assertEqual(process.wait(timeout=5), 0)

    def test_concurrent_same_side_workspace_is_created_once(self):
        self.start()
        other = self.field("pane_id", "=tmx:1.2")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.launch("workspace", "3", "left"), range(4)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.active(), "3:1")
        self.assertEqual(len(self.snapshot().splitlines()), 3)
        self.assertEqual(self.field("pane_id", "=tmx:1.2"), other)

    def test_workspace_switch_keeps_shell_state_history_and_copy_position(self):
        process, terminal = self.scroll_fixture()
        original = self.field("pane_id")
        other = self.field("pane_id", "=tmx:1.2")
        os.write(terminal, b"\x09")
        self.until(lambda: self.field("scroll_position") == "1")
        os.write(terminal, b"\x1b[50;5u")  # Ctrl+2 while in copy mode.
        self.until(lambda: self.active() == "2:1")
        saved = self.root / "workspace-marker"
        self.mux("send-keys", "-t", "=tmx:1.1", "TMX_VALUE=retained", "Enter")
        os.write(terminal, b"\x021")
        self.until(lambda: self.active() == "1:1")
        self.assertEqual(self.field("pane_id"), original)
        self.assertEqual(self.field("scroll_position"), "1")
        self.assertEqual(self.field("pane_id", "=tmx:1.2"), other)
        os.write(terminal, b"\x022")
        self.until(lambda: self.active() == "2:1")
        self.mux("send-keys", "-t", "=tmx:1.1",
                 f'printf "%s" "$TMX_VALUE" > {saved}', "Enter")
        self.until(saved.exists)
        self.assertEqual(saved.read_text(), "retained")
        os.write(terminal, b"\x02d")
        self.assertEqual(process.wait(timeout=5), 0)

    def test_migration_preserves_all_old_panes_processes_and_active_window(self):
        # Construct the previous two-windows/two-panes layout directly, without
        # relying on the implementation of the new launcher.
        config = self.root / "old.conf"
        config.write_text("set -g base-index 1\nsetw -g pane-base-index 1\n"
                          "setw -g remain-on-exit on\nset -g default-shell /bin/bash\n")
        subprocess.run(["tmux", "-L", self.socket, "-f", str(config),
                        "new-session", "-d", "-s", "tmx", "-n", "old-1"],
                       env=self.env, check=True)
        self.mux("split-window", "-d", "-h", "-t", "=tmx:1")
        self.mux("new-window", "-d", "-t", "tmx:2", "-n", "old-2")
        self.mux("split-window", "-d", "-h", "-t", "=tmx:2")
        self.mux("select-window", "-t", "=tmx:2")
        self.mux("select-pane", "-t", "=tmx:2.2")
        original = self.snapshot()
        second_left = self.field("pane_id", "=tmx:2.1")
        first_right = self.field("pane_id", "=tmx:1.2")
        self.start()
        self.assertEqual(self.snapshot(), original)
        self.assertEqual(self.active(), "5:2")
        self.assertEqual(self.launch("workspace", "4", "right").returncode, 0)
        self.assertEqual(self.field("pane_id", "=tmx:2.1"), second_left)
        self.assertEqual(self.field("pane_id", "=tmx:2.2"), first_right)
        self.assertEqual(self.active(), "4:2")
        # Every original process and pane still exists after swapping.
        before = sorted((line.split()[1], line.split()[2]) for line in original.splitlines())
        after = sorted((line.split()[1], line.split()[2]) for line in self.snapshot().splitlines())
        self.assertEqual(before, after)
        self.assertEqual(self.field("window_panes", "=tmx:2.1"), "2")

    def test_exit_reopens_a_fresh_shell_in_the_same_workspace(self):
        self.start()
        pane = "=tmx:1.1"
        original_id = self.field("pane_id")
        other_pid = self.field("pane_pid", "=tmx:1.2")
        old_pid = self.field("pane_pid")
        self.mux("send-keys", "-t", pane, "TMX_EXIT_VALUE=gone; exit", "Enter")
        self.until(lambda: self.field("pane_dead") == "0" and self.field("pane_pid") != old_pid)
        self.assertEqual(self.field("pane_id"), original_id)
        self.assertEqual(self.field("@tmx_workspace"), "1")
        self.assertEqual(self.field("@tmx_side"), "left")
        self.assertEqual(self.field("pane_pid", "=tmx:1.2"), other_pid)
        self.assertEqual(len(self.snapshot().splitlines()), 2)
        self.until(lambda: self.field("pane_current_command") == "bash")
        self.assertNotIn("TMX_EXIT_VALUE=gone", self.mux("capture-pane", "-p", "-t", pane))
        marker = self.root / "fresh-shell"
        self.mux("send-keys", "-t", pane,
                 f'printf "%s" "${{TMX_EXIT_VALUE-unset}}" > {marker}', "Enter")
        self.until(marker.exists)
        self.assertEqual(marker.read_text(), "unset")
        # Repeated exits keep reopening shells; the hook survives respawn.
        old_pid = self.field("pane_pid")
        self.mux("send-keys", "-t", pane, "C-d")
        self.until(lambda: self.field("pane_dead") == "0" and self.field("pane_pid") != old_pid)
        before = self.snapshot()
        self.assertEqual(self.launch("reopen", original_id).returncode, 0)
        self.assertEqual(self.snapshot(), before)  # Manual retry cannot kill live shells.

    def test_background_command_exit_opens_shell_without_rerunning_command(self):
        self.start()
        original = self.field("pane_id")
        self.start("2")  # Park workspace 1; it must reopen without stealing focus.
        focus = self.active()
        pid = self.field("pane_pid", original)
        marker = self.root / "runs"
        command = f'printf x >> {marker}; sleep 0.1; exit 9'
        self.mux("respawn-pane", "-k", "-t", original, "/bin/sh", "-c", command)
        self.until(lambda: marker.exists() and self.field("pane_current_command", original) == "bash"
                   and self.field("pane_pid", original) != pid)
        self.assertEqual(marker.read_text(), "x")
        self.assertEqual(self.field("pane_dead", original), "0")
        self.assertEqual(self.field("@tmx_workspace", original), "1")
        self.assertEqual(self.active(), focus)
        self.assertEqual(len(self.snapshot().splitlines()), 3)

    def test_fixed_ranges_bound_workspace_count_under_concurrency(self):
        self.start()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda n: self.launch("--detached", str(n)), (2, 3, 5, 6)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.snapshot().splitlines()), 6)
        before = self.snapshot()
        for number in ("7", "8", "9"):
            self.assertEqual(self.launch("--detached", number).returncode, 2)
        self.assertEqual(self.snapshot(), before)
        # Exact numbers always address their fixed side, regardless of focus.
        self.assertEqual(self.launch("workspace", "1").returncode, 0)
        self.assertEqual(self.active(), "1:1")
        left = self.field("pane_id")
        self.assertEqual(self.launch("workspace", "6").returncode, 0)
        self.assertEqual(self.active(), "6:2")
        self.assertEqual(self.field("pane_id"), left)

    def test_history_and_foreign_tmux_rejection(self):
        self.start()
        self.mux("send-keys", "-t", "=tmx:1.1", "echo preserved-history", "Enter")
        self.until(lambda: "preserved-history" in self.launch("history").stdout)
        self.env["TMUX"] = "/tmp/foreign-server,123,0"
        result = self.launch("--detached")
        self.assertEqual(result.returncode, 2)
        self.assertIn("detach from the other tmux", result.stderr)

    def scroll_fixture(self, mode="inline", agent=None):
        """A raw terminal UI that advertises its protocol and records input."""
        self.start()
        process, terminal = self.attach()
        # Behave like a real terminal: continuously consume redraws so its PTY
        # output buffer cannot fill and prevent the client from exiting.
        finished = threading.Event()
        def drain():
            try:
                while not finished.is_set():
                    if select.select([terminal], [], [], 0.05)[0]:
                        if not os.read(terminal, 65536):
                            return
            except OSError:
                pass
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        def stop_reader():
            finished.set()
            reader.join(timeout=1)
        self.addCleanup(stop_reader)
        app = self.root / "terminal_fixture.py"
        if agent is None:
            agent = "muse" if mode == "inline" else "codex" if mode == "alternate" else "claude"
        interpreter = self.root / agent
        shutil.copy2(sys.executable, interpreter)
        app.write_text('''import os, pathlib, sys, tty
p = pathlib.Path(sys.argv[1])
mode = sys.argv[2]
tty.setraw(0)
for i in range(300):
    print(f"retained-line-{i:03d}", end="\\r\\n", flush=True)
if mode != "inline":
    sys.stdout.write("\\x1b[?1049h")
if mode in ("sgr", "legacy", "utf8"):
    sys.stdout.write("\\x1b[?1000h")
if mode == "sgr":
    sys.stdout.write("\\x1b[?1006h")
if mode == "utf8":
    sys.stdout.write("\\x1b[?1005h")
sys.stdout.flush()
with (p / "input").open("ab", buffering=0) as output:
    (p / "ready").touch()
    while True:
        data = os.read(0, 4096)
        if not data:
            break
        output.write(data)
''')
        command = "exec " + " ".join(map(shlex.quote, (str(interpreter), str(app), str(self.root), mode)))
        self.mux("send-keys", "-t", "=tmx:1.1", command, "Enter")
        self.until(lambda: (self.root / "ready").exists())
        self.until(lambda: self.field("history_size") and int(self.field("history_size")) > 200)
        if mode != "inline":
            self.until(lambda: self.field("alternate_on") == "1")
        self.until(lambda: self.field("pane_current_command") == agent)
        return process, terminal

    def field(self, name, target="=tmx:1.1"):
        return self.mux("display-message", "-p", "-t", target, "#{" + name + "}")

    def test_scroll_history_repeats_bottom_and_pane_workspace_isolation(self):
        process, terminal = self.scroll_fixture()
        os.write(terminal, b"\x09")  # Ctrl-i / Tab.
        self.until(lambda: self.field("scroll_position") == "1")
        for _ in range(10):
            os.write(terminal, b"\x09")
            time.sleep(0.02)  # Hardware-style typematic repeat.
        # SSH may deliver accumulated hardware repeats in a single packet.
        os.write(terminal, b"\x09" * 4)
        time.sleep(0.15)
        position = int(self.field("scroll_position"))
        self.assertGreaterEqual(position, 1)
        self.assertLessEqual(position, 3)
        time.sleep(0.3)
        self.assertEqual(int(self.field("scroll_position")), position)
        self.assertEqual(self.field("pane_in_mode", "=tmx:1.2"), "0")
        # Pane and workspace navigation still work while reading history.
        os.write(terminal, b"\x0f")
        self.until(lambda: self.active() == "4:2")
        os.write(terminal, b"\x0f")
        self.until(lambda: self.active() == "1:1")
        os.write(terminal, b"\x1b[50;5u")
        self.until(lambda: self.active() == "2:1")
        os.write(terminal, b"\x1b[49;5u")
        self.until(lambda: self.active() == "1:1")
        self.assertEqual(int(self.field("scroll_position")), position)
        for _ in range(position + 2):
            os.write(terminal, b"\x0b")
            time.sleep(0.27)
        self.until(lambda: self.field("pane_in_mode") == "0")
        # Extra down repeats must not leak Ctrl-k into the application.
        self.assertEqual((self.root / "input").read_bytes(), b"")
        os.write(terminal, b"\x02t")
        self.until(lambda: (self.root / "input").read_bytes() == b"\x09")
        os.write(terminal, b"\x02d")
        self.assertEqual(process.wait(timeout=5), 0)

    def check_scroll_acceleration(self, mode):
        process, terminal = self.scroll_fixture(mode)
        history = mode == "inline"
        if history:
            self.mux("copy-mode", "-t", "=tmx:1.1")
            self.mux("send-keys", "-t", "=tmx:1.1", "-X", "-N", "100", "scroll-up")

        def progress():
            if history:
                return int(self.field("scroll_position"))
            return (self.root / "input").read_bytes().count(b"M")

        for key in (b"\x09", b"\x0b"):
            with self.subTest(mode=mode, direction=key):
                # Feed real client bytes for over two seconds, not mocked clocks.
                start = time.monotonic()
                snapshots = []
                checkpoints = iter((1.0, 1.8, 2.2, 3.0))
                checkpoint = next(checkpoints)
                while True:
                    os.write(terminal, key)
                    elapsed = time.monotonic() - start
                    if elapsed >= checkpoint:
                        snapshots.append(progress())
                        checkpoint = next(checkpoints, None)
                        if checkpoint is None:
                            break
                    time.sleep(0.02)
                slow = abs(snapshots[1] - snapshots[0])
                fast = abs(snapshots[3] - snapshots[2])
                self.assertGreater(slow, 0)
                self.assertGreaterEqual(fast, slow * 2, (mode, snapshots))
                # Let the final in-flight key finish, then ensure input stopping
                # also stops output. Idle must reset acceleration for another tap.
                time.sleep(0.08)
                stopped = progress()
                time.sleep(0.65)
                self.assertEqual(progress(), stopped)
                os.write(terminal, key)
                self.until(lambda: abs(progress() - stopped) == 1)
                for _ in range(3):
                    os.write(terminal, key)
                    time.sleep(0.02)
                time.sleep(0.03)
                self.assertEqual(abs(progress() - stopped), 1)
        self.assertEqual(self.field("pane_in_mode", "=tmx:1.2"), "0")
        os.write(terminal, b"\x02d")
        self.assertEqual(process.wait(timeout=5), 0)

    def test_history_accelerates_both_directions_after_two_seconds(self):
        self.check_scroll_acceleration("inline")

    def test_codex_wheel_accelerates_both_directions_after_two_seconds(self):
        self.check_scroll_acceleration("alternate")

    def test_claude_wheel_accelerates_both_directions_after_two_seconds(self):
        self.check_scroll_acceleration("sgr")

    def test_mouse_wheel_sgr_and_manual_history_override(self):
        process, terminal = self.scroll_fixture("sgr")
        self.until(lambda: self.field("mouse_any_flag") == "1")
        column = (int(self.field("pane_width")) + 1) // 2
        row = (int(self.field("pane_height")) + 1) // 2
        up = f"\x1b[<64;{column};{row}M".encode()
        down = f"\x1b[<65;{column};{row}M".encode()
        os.write(terminal, b"\x09")
        self.until(lambda: (self.root / "input").read_bytes() == up)
        os.write(terminal, b"\x0b")
        self.until(lambda: (self.root / "input").read_bytes() == up + down)
        self.assertEqual(self.field("pane_in_mode"), "0")
        # Explicit tmux history takes precedence even if the TUI wants the mouse.
        self.mux("copy-mode", "-t", "=tmx:1.1")
        os.write(terminal, b"\x09")
        self.until(lambda: self.field("scroll_position") == "1")
        self.assertEqual((self.root / "input").read_bytes(), up + down)
        os.write(terminal, b"q")
        self.until(lambda: self.field("pane_in_mode") == "0")
        os.write(terminal, b"\x02d")
        self.assertEqual(process.wait(timeout=5), 0)

    def test_mouse_wheel_legacy(self):
        process, terminal = self.scroll_fixture("legacy")
        self.until(lambda: self.field("mouse_any_flag") == "1")
        column = min(95, (int(self.field("pane_width")) + 1) // 2)
        row = min(95, (int(self.field("pane_height")) + 1) // 2)
        up = b"\x1b[M" + bytes((96, column + 32, row + 32))
        down = b"\x1b[M" + bytes((97, column + 32, row + 32))
        os.write(terminal, b"\x09")
        self.until(lambda: (self.root / "input").read_bytes() == up)
        os.write(terminal, b"\x0b")
        self.until(lambda: (self.root / "input").read_bytes() == up + down)
        os.write(terminal, b"\x02d")
        self.assertEqual(process.wait(timeout=5), 0)

    def test_codex_fullscreen_without_mouse_uses_sgr_not_pages(self):
        process, terminal = self.scroll_fixture("alternate")
        self.assertEqual(self.launch("app").stdout.strip(), "codex")
        column = (int(self.field("pane_width")) + 1) // 2
        row = (int(self.field("pane_height")) + 1) // 2
        up = f"\x1b[<64;{column};{row}M".encode()
        down = f"\x1b[<65;{column};{row}M".encode()
        os.write(terminal, b"\x1b[105;5u")  # Distinct extended Ctrl-i.
        self.until(lambda: (self.root / "input").read_bytes() == up)
        for _ in range(4):
            os.write(terminal, b"\x09")
            time.sleep(0.03)
        self.assertEqual((self.root / "input").read_bytes(), up)
        time.sleep(0.8)
        os.write(terminal, b"\x09")
        self.until(lambda: (self.root / "input").read_bytes() == up + up)
        os.write(terminal, b"\x0b")
        self.until(lambda: (self.root / "input").read_bytes() == up + up + down)
        self.assertEqual(self.field("pane_in_mode"), "0")
        os.write(terminal, b"\x02d")
        self.assertEqual(process.wait(timeout=5), 0)

    def test_unknown_fullscreen_does_not_receive_guessed_keys(self):
        process, terminal = self.scroll_fixture("alternate", agent="unknown-tui")
        self.assertEqual(self.launch("app").stdout.strip(), "other")
        os.write(terminal, b"\x09\x0b")
        time.sleep(0.2)
        self.assertEqual((self.root / "input").read_bytes(), b"")
        self.assertEqual(self.field("pane_in_mode"), "0")
        os.write(terminal, b"\x02d")
        self.assertEqual(process.wait(timeout=5), 0)

    def test_claude_without_mouse_does_not_receive_guessed_keys(self):
        process, terminal = self.scroll_fixture("alternate", agent="claude")
        self.assertEqual(self.launch("app").stdout.strip(), "claude")
        os.write(terminal, b"\x09\x0b")
        time.sleep(0.2)
        self.assertEqual((self.root / "input").read_bytes(), b"")
        os.write(terminal, b"\x02d")
        self.assertEqual(process.wait(timeout=5), 0)

    def test_versioned_muse_binary_is_detected(self):
        process, terminal = self.scroll_fixture(agent="muse-bin-1.4.0")
        self.assertEqual(self.launch("app").stdout.strip(), "muse")
        os.write(terminal, b"\x09")
        self.until(lambda: self.field("scroll_position") == "1")
        os.write(terminal, b"q")
        self.until(lambda: self.field("pane_in_mode") == "0")
        os.write(terminal, b"\x02d")
        self.assertEqual(process.wait(timeout=5), 0)

    def test_ctrl_q_sends_shift_left_only_to_foreground_codex(self):
        process, terminal = self.scroll_fixture(mode="alternate", agent="codex")
        os.write(terminal, b"\x11")
        self.until(lambda: (self.root / "input").read_bytes() == b"\x1b[1;2D")
        # Another program on the right must not inherit Codex's mapping.
        other = self.root / "right-input"
        script = self.root / "raw_reader.py"
        script.write_text("import tty,os,pathlib,sys; tty.setraw(0); "
                          "pathlib.Path(sys.argv[1]).write_bytes(os.read(0,1))\n")
        self.mux("send-keys", "-t", "=tmx:1.2",
                 shlex.join([sys.executable, str(script), str(other)]), "Enter")
        self.until(lambda: self.field("pane_current_command", "=tmx:1.2").startswith("python"))
        os.write(terminal, b"\x0f")
        self.until(lambda: self.active() == "4:2")
        os.write(terminal, b"\x11")
        self.until(other.exists)
        self.assertEqual(other.read_bytes(), b"\x11")
        self.assertEqual((self.root / "input").read_bytes(), b"\x1b[1;2D")
        os.write(terminal, b"\x02d")
        self.assertEqual(process.wait(timeout=5), 0)

    def test_ctrl_q_passes_through_claude_and_muse(self):
        for agent in ("claude", "muse"):
            with self.subTest(agent=agent):
                # Isolate fixtures so each starts with a clean terminal stream.
                child = TmxTests()
                child.setUp()
                try:
                    process, terminal = child.scroll_fixture(agent=agent)
                    os.write(terminal, b"\x11")
                    child.until(lambda: (child.root / "input").read_bytes() == b"\x11")
                    os.write(terminal, b"\x02d")
                    self.assertEqual(process.wait(timeout=5), 0)
                finally:
                    child.doCleanups()

    def test_interpreter_detection_checks_script_not_prompt_arguments(self):
        self.start()
        # A Python ELF copied as node lets the fixture exercise /proc executable
        # and argv classification without requiring a Node installation.
        interpreter = self.root / "node"
        shutil.copy2(sys.executable, interpreter)
        for script_name, expected in [
            ("node_modules/@anthropic-ai/claude-code/cli.js", "claude"),
            ("node_modules/@openai/codex/bin/codex.js", "codex"),
            ("unrelated.py", "other"),
        ]:
            script = self.root / script_name
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text("import time; time.sleep(60)\n")
            self.mux("respawn-pane", "-k", "-t", "=tmx:1.1",
                     str(interpreter), str(script), "codex", "claude", "muse")
            self.until(lambda: self.launch("app").stdout.strip() == expected)

    def test_opt_in_inline_codex_launch_preserves_arguments(self):
        self.start()
        executable = self.root / "codex"
        record = self.root / "codex-args"
        executable.write_text("#!/usr/bin/env python3\nimport pathlib,sys,json\n"
                              + f"pathlib.Path({str(record)!r}).write_text(json.dumps(sys.argv[1:]))\n")
        executable.chmod(0o700)
        arguments = ["resume", "--last", "literal ' quote; $(not-a-command)"]
        self.mux("respawn-pane", "-k", "-t", "=tmx:1.1", "/usr/bin/env",
                 "PATH=" + str(self.root) + ":" + os.environ["PATH"],
                 str(SCRIPT), "run", "codex", *arguments)
        self.until(record.exists)
        import json
        self.assertEqual(json.loads(record.read_text()),
                         ["--no-alt-screen", "-c", "tui.fullscreen_transcript=false", *arguments])


if __name__ == "__main__":
    unittest.main()
