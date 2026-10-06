import os
import select
import shlex
import shutil
import subprocess
import threading
import unittest
import uuid

import test_tmx


@unittest.skipUnless(shutil.which("tmux") and shutil.which("flock"), "tmux and flock required")
class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.t = test_tmx.TmxTests("test_sh_bash_workspace_creation_and_reuse")
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)

    def identities(self, windows=True):
        return sorted(self.t.mux("list-panes", "-s", "-F",
                                 "#{pane_id} #{pane_pid} " + ("#{window_id} " if windows else "") +
                                 "#{@tmx_side} #{@tmx_workspace}").splitlines())

    def visible(self, side):
        rows = self.t.mux("list-panes", "-t", "=tmx:1", "-F", "#{@tmx_side} #{pane_id}")
        return next(row.split()[1] for row in rows.splitlines() if row.startswith(side + " "))

    def focused(self):
        return self.t.field("pane_id", "=tmx:")

    def geometry(self, vertical):
        left, right = self.visible("left"), self.visible("right")
        lx, ly = int(self.t.field("pane_left", left)), int(self.t.field("pane_top", left))
        rx, ry = int(self.t.field("pane_left", right)), int(self.t.field("pane_top", right))
        return lx == rx == 0 and ly < ry if vertical else ly == ry == 0 and lx < rx

    def attach(self):
        process, terminal = self.t.attach()
        # Consume terminal redraws, as an actual iSH client would do.
        stopped = threading.Event()
        def drain():
            try:
                while not stopped.is_set():
                    if select.select([terminal], [], [], 0.05)[0]:
                        if not os.read(terminal, 65536):
                            return
            except OSError:
                pass
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        def stop():
            stopped.set()
            reader.join(timeout=1)
        self.addCleanup(stop)
        return process, terminal

    def toggle(self, terminal, vertical):
        os.write(terminal, b"\x1by")
        self.t.until(lambda: self.t.field("@tmx_vertical") == str(int(vertical))
                     and self.geometry(vertical))
        self.assertEqual(self.t.mux("show-option", "-qv", "-t", "=tmx:", "status-position"),
                         "bottom" if vertical else "top")
        self.assertEqual(self.t.mux("show-option", "-wqv", "-t", "=tmx:1", "pane-border-status"),
                         "top" if vertical else "off")

    def test_raw_option_y_repeated_toggles_preserve_all_processes_and_focus_after_swaps(self):
        self.t.start()
        for number in (2, 3, 7, 8):
            self.t.start(str(number))
        before = self.identities()
        focus = self.focused()
        process, terminal = self.attach()
        for vertical in (True, False, True, False, True, False):
            self.toggle(terminal, vertical)
            self.assertEqual(self.identities(), before)
            self.assertEqual(self.focused(), focus)
            self.assertEqual(self.t.field("window_panes"), "2")
        os.write(terminal, b"\x02d")
        self.assertEqual(process.wait(timeout=5), 0)

    def test_stacked_workspaces_cycle_independently_and_new_workspaces_stay_on_their_side(self):
        self.t.start()
        process, terminal = self.attach()
        self.toggle(terminal, True)
        # Create new slots while stacked, using actual prefix + digit bytes.
        for number in (2, 3, 7, 8):
            other = self.visible("right" if number < 6 else "left")
            os.write(terminal, b"\x02" + str(number).encode())
            self.t.until(lambda: self.t.field("@tmx_workspace", "=tmx:") == str(number))
            self.assertEqual(self.visible("right" if number < 6 else "left"), other)
            self.assertTrue(self.geometry(True))
        before = self.identities(windows=False)
        for side, sequence in (("left", (1, 2, 3, 1)), ("right", (6, 7, 8, 6))):
            self.t.mux("select-pane", "-t", self.visible(side))
            other = self.visible("right" if side == "left" else "left")
            for number in sequence:
                os.write(terminal, b"\x1bp")
                self.t.until(lambda: self.t.field("@tmx_workspace", "=tmx:") == str(number))
                self.assertEqual(self.visible("right" if side == "left" else "left"), other)
                self.assertTrue(self.geometry(True))
        self.assertEqual(self.identities(windows=False), before)
        os.write(terminal, b"\x1bo")
        self.t.until(lambda: self.t.field("@tmx_side", "=tmx:") == "left")
        before = self.identities()
        self.toggle(terminal, False)
        self.assertEqual(self.identities(), before)
        os.write(terminal, b"\x02d")
        self.assertEqual(process.wait(timeout=5), 0)

    def test_saved_vertical_mode_survives_disconnect_and_relaunch(self):
        self.t.start("3")
        self.t.start("8")
        before = self.identities()
        focus = self.focused()
        process, terminal = self.attach()
        self.toggle(terminal, True)
        self.t.close_fd(terminal)
        process.wait(timeout=5)
        self.assertEqual(self.t.launch("--detached").returncode, 0)
        self.assertTrue(self.geometry(True))
        self.assertEqual(self.identities(), before)
        self.assertEqual(self.focused(), focus)
        second, terminal2 = self.attach()
        self.assertTrue(self.geometry(True))
        self.assertEqual(self.t.mux("show-option", "-qv", "-t", "=tmx:", "status-position"), "bottom")
        self.assertEqual(self.focused(), focus)
        self.toggle(terminal2, False)
        os.write(terminal2, b"\x02d")
        self.assertEqual(second.wait(timeout=5), 0)

    def test_option_y_works_in_both_history_tables_without_restarting_the_app(self):
        process, terminal = self.t.scroll_fixture()
        before = self.identities()
        for mode in ("vi", "emacs"):
            self.t.mux("set-option", "-w", "mode-keys", mode)
            self.t.mux("set-option", "-pu", "-t", "=tmx:1.1", "@tmx_scroll_state")
            os.write(terminal, b"\x1bi")
            self.t.until(lambda: self.t.field("pane_in_mode") == "1")
            self.toggle(terminal, True)
            self.assertEqual(self.t.field("pane_in_mode"), "1")
            self.assertEqual(self.identities(), before)
            self.toggle(terminal, False)
            self.assertEqual(self.t.field("pane_in_mode"), "1")
            self.assertEqual(self.identities(), before)
            self.t.mux("send-keys", "-X", "cancel")
        self.assertEqual((self.t.root / "input").read_bytes(), b"")

    def test_fullscreen_agent_process_and_terminal_modes_survive_toggling(self):
        for mode, agent in (("alternate", "codex"), ("sgr", "claude")):
            with self.subTest(agent=agent):
                fixture = test_tmx.TmxTests("test_sh_bash_workspace_creation_and_reuse")
                fixture.setUp()
                try:
                    process, terminal = fixture.scroll_fixture(mode, agent)
                    before = fixture.mux("list-panes", "-s", "-F", "#{pane_id} #{pane_pid}")
                    for vertical in (1, 0, 1, 0):
                        os.write(terminal, b"\x1by")
                        fixture.until(lambda: fixture.field("@tmx_vertical") == str(vertical)
                                      and fixture.mux("show-option", "-qv", "-t", "=tmx:", "status-position") == ("bottom" if vertical else "top"))
                        self.assertEqual(fixture.mux("list-panes", "-s", "-F", "#{pane_id} #{pane_pid}"), before)
                        self.assertEqual(fixture.field("alternate_on"), "1")
                        self.assertEqual(fixture.field("pane_current_command"), agent)
                        self.assertEqual(fixture.field("mouse_any_flag"), "1" if mode == "sgr" else "0")
                    self.assertEqual((fixture.root / "input").read_bytes(), b"")
                finally:
                    fixture.doCleanups()

    def test_vertical_headers_follow_their_panes_and_plugins_appear_only_in_footer(self):
        self.t.start()
        for number in (2, 3, 7, 8):
            self.t.start(str(number))
        self.t.mux("select-pane", "-t", self.visible("left"))
        before = self.identities()
        self.t.mux("set-option", "-g", "@tmx_storage", "#[fg=white]sda: 78%")
        self.t.mux("set-option", "-g", "@tmx_weather", "#[fg=colour220]☀️#[fg=white] 24° #[fg=colour141]UV#[fg=white] 5")
        outer = "tmx-test-vertical-view-" + uuid.uuid4().hex
        def view(*args):
            return subprocess.check_output(["tmux", "-L", outer, *args], text=True, timeout=5)
        self.addCleanup(lambda: subprocess.run(["tmux", "-L", outer, "kill-server"], capture_output=True))
        command = shlex.join(["/usr/bin/env", "-u", "TMUX", "-u", "TMUX_PANE",
                              "TMX_SOCKET=" + self.t.socket, "TMX_STATUS_DISABLED=1",
                              "XDG_STATE_HOME=" + self.t.env["XDG_STATE_HOME"], str(test_tmx.SCRIPT)])
        view("-f", "/dev/null", "new-session", "-d", "-s", "view", "-x", "100", "-y", "40", command)
        view("set-option", "-g", "status", "off")
        self.t.until(lambda: self.t.mux("list-clients", "-F", "#{client_tty}") != "")
        def screen(ansi=False):
            args = ["capture-pane", "-p", "-N", "-t", "=view:"]
            if ansi:
                args.insert(1, "-e")
            return view(*args).splitlines()
        view("send-keys", "-t", "=view:", "M-y")
        self.t.until(lambda: self.geometry(True) and "sda:" in screen()[-1])
        for side in ("left", "right"):
            self.t.mux("select-pane", "-t", self.visible(side))
            top = int(self.t.field("pane_top", self.visible("left"))) - 1
            bottom = int(self.t.field("pane_top", self.visible("right"))) - 1
            active_row = top if side == "left" else bottom
            self.t.until(lambda: "\x1b[31m●" in screen(True)[active_row])
            rows, ansi = screen(), screen(True)
            self.assertIn("[1] [2] [●]", rows[top])
            self.assertIn("[1] [2] [●]", rows[bottom])
            for row in (rows[top], rows[bottom], rows[-1]):
                start, end = row.index("["), row.rindex("]") + 1
                self.assertLessEqual(abs(start - (100 - end)), 1)
            self.assertNotIn("sda:", rows[top] + rows[bottom])
            self.assertNotIn("UV", rows[top] + rows[bottom])
            self.assertIn("sda: 78% | ☀️ 24° UV 5 |", rows[-1])
            self.assertRegex(rows[-1], r"\d{2}/\d{2} \d{2}:\d{2} \] \s*$")
            self.assertNotIn("\x1b[31m●", ansi[bottom if side == "left" else top])
        view("send-keys", "-t", "=view:", "M-y")
        self.t.until(lambda: self.geometry(False) and "sda:" in screen()[0])
        self.assertNotIn("sda:", screen()[-1])
        self.assertEqual(self.identities(), before)

    def test_layout_refuses_external_extra_panes_without_touching_processes(self):
        self.t.start()
        self.t.mux("split-window", "-d", "-t", "=tmx:1")
        before = self.identities()
        result = self.t.launch("layout")
        self.assertEqual(result.returncode, 2)
        self.assertIn("visible layout was modified", result.stderr)
        self.assertEqual(self.identities(), before)
