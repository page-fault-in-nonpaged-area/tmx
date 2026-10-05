# tmx

Run `tmx` from bash or sh on dev@dev. The command points to `tmx.sh` in this
directory. Requires Linux, tmux 3.2+, bash, readlink and flock. The optional
weather helper and the tests also use Python 3 (standard library only). Installation is private to dev, with owner-only permissions.

`tmx` opens two side-by-side terminals: **left** and **right**. Each side has its
own set of **at most three workspaces**: **1–3 belong to the left**, and
**4–6 belong to the right**. Numbers are global: selecting 2 always changes the
left; selecting 5 always changes the right, regardless of the current focus.
There are no workspaces 7–9. The three-workspace limit includes inactive ones.
Each pane's top bar displays local labels 1–3, for example `[x] [2] [3]`.
On the right these represent fixed workspace IDs 4–6; only the displayed labels
change. Ctrl-b + 4/5/6 still selects the right-hand workspaces.
The active workspace is marked by `x`: red in the focused pane, white in the
inactive pane. The other labels keep their local display numbers. Numbers and brackets stay white. A teal-blue horizontal line
indicates the focused side, including the first two strokes. Inactive horizontal borders, the vertical divider, and the `┬` junction are gray. These bars share one full-width top status row; there is no bottom
status row. Header sections are explicitly ordered left then right, so swapping
workspace terminals cannot move a header across the divider. This also avoids
tmux's reserved two-character pane-header margin. The
right side has one right-aligned block, for example:
`[ sda: 78% | SUNY 24° UV 5 | 10/05 12:12 ]`.

`sda:` is a fixed lowercase white label for the filesystem mounted at `/`.
Its percentage is **free space available to your user / total filesystem
space**, rounded to a whole percent; root-reserved blocks do not count as
available. The percentage is bright pinkish red below 30%, orange from 30–49%, and green
at 50% or higher. It refreshes every minute. The clock keeps its gold date, green
24-hour time, and host timezone, refreshing every second using native tmux
formats. Brackets and `|` separators are white.

If weather fails, only that segment and its separator disappear. At narrow
widths, weather is omitted first, then storage, then the clock if necessary,
so workspace labels are never overwritten.

Temperatures are Celsius; weather numbers and brackets are white. Conditions use
four capital letters: `SUNY` gold, `RAIN`/`DRZL` bright blue, `SNOW` pale blue,
`STRM` magenta, `SLET` cyan, and `CLDY`/`OVRC`/`FOGG`/`MIST` gray. `PART`
is pale gold; `CLER` is pale blue for a clear night. `UV` always stays purple.
Weather hides before storage or the clock when space is tight.

The separate `weather.py` helper reads `weather.json`, initially set to Toronto.
Change its city and matching latitude/longitude to use another location; the
worker notices configuration changes within a minute. It checks
[wttr.in](https://github.com/chubin/wttr.in#json-output) hourly, with
[Open-Meteo](https://open-meteo.com/en/docs) as an independent fallback. The
fallback uses the current hour's UV forecast, not the day's maximum.
If both sources fail or the data is invalid, the entire weather segment is
hidden. Failed checks are cached too, so an outage never triggers requests on
every redraw; the next normal attempt is an hour later.

The separate `status.py` worker starts when a client attaches. It reads free
space and calls the modular `weather.py` helper, publishing values directly to tmux; typing, scrolling and clock redraws do not
launch weather commands. State lives under `${XDG_STATE_HOME:-~/.local/state}/tmx/`.
The worker continues hourly checks while detached and exits after its tmux
session ends. The main `tmx.sh` has no status-plugin logic. Without Python 3, storage and
weather are absent; the clock still works. `python3 weather.py --once` performs a standalone check and
prints the styled weather segment (or a blank line on failure).


Later launches reconnect to the same two visible terminals, selected workspaces,
focused side, running programs and retained history. Run any program, including
`codex`, normally in either side. `tmx 3` creates or selects workspace 3 in the
left pane. On the first launch, `tmx 3` starts left:3 and right:4.

A session registry anchors each workspace number to its stable tmux pane ID.
Ctrl-p resolves the currently focused terminal in the permanent visible window
under the workspace lock, rather than trusting tmux's potentially parked key
context. Visible left/right positions must match the registry before anything
moves. Shell respawns retain the same pane ID and workspace assignment.

Inactive workspaces stay alive in background tmux windows. The wrapper swaps
individual terminals into the two visible positions; it does not switch the
whole window. Additional workspaces therefore use additional background
terminals, while exactly two are visible. There is no nested tmux layer.
Existing installations migrate their old left/right terminals to the matching
side, retaining every process and the current view. Existing left workspaces
are numbered 1–3 and right workspaces 4–6 in their previous numeric order.
For the previous full 1–3 layout, the right side becomes 4, 5, 6 respectively.

Press **Ctrl-o** to cycle forward through the two panes: left → right → left.
Press **Ctrl-p** to cycle forward through the **existing workspaces in the
focused pane**, in numeric order, wrapping back to the lowest number. For
example, left cycles 1 → 2 → 3 → 1 and right cycles 4 → 5 → 6 → 4.
Missing workspaces are skipped. The other side stays
unchanged. If the side has only one workspace, Ctrl-p stays there; it never
creates new workspaces.

Press **Ctrl-b**, release it, then **1–6** to create or select an exact workspace
on its fixed side. Selecting 1–3 focuses the left; 4–6 focuses the right. These controls also work while reading scrollback.

Press **Ctrl-q** to send **Shift+Left** when the focused pane is running Codex
(for its questions UI). For other foreground programs it sends ordinary Ctrl-q.
Detection uses the same foreground-process classifier as scrolling, including
the Node-based Codex launcher. Ctrl-b then q/w remains available in every app.

Press **Ctrl-i** to scroll up and **Ctrl-k** to scroll down, with no prefix.
Holding either key repeats scrolling when the keyboard/iSH sends key repeats;
the launcher does not invent a continuous timer without key-release events.
Scrolling applies only to the active pane. App-specific handlers target exactly
**Codex, Claude Code, and Muse Code**. `tmx app` prints the detected app for the
current pane. Detection uses tmux's foreground command, with a foreground-process
group check for the known OpenAI/Anthropic Node or Bun launcher paths. It does
not match agent names in prompt arguments or unrelated background processes.

| App/view | Scrolling behavior |
| --- | --- |
| Codex fullscreen | One SGR wheel event, even when mouse reporting is off; tested Codex 0.160.0 moves **three rows**, not one |
| Codex inline | Exactly one line of tmux history |
| Claude fullscreen with mouse reporting | Native wheel action using its requested SGR/legacy protocol; Claude documents `scroll:lineUp` / `scroll:lineDown` |
| Claude inline | Exactly one line of tmux history |
| Muse inline | Exactly one line of tmux history; Muse 1.4.0 uses this renderer |
| Other ordinary terminal output | Exactly one line of tmux history |

PageUp/PageDown injection has been removed. Unrecognized fullscreen programs,
Muse in an unverified fullscreen mode, and Claude fullscreen without mouse
reporting show an unsupported-view message instead of receiving guessed keys.
Ctrl-b then `u` explicitly opens retained terminal history regardless of app.
While in that history, `q` exits and scrolling down to the bottom returns live.
Ctrl-k at the ordinary live bottom does nothing.

The first press responds immediately. Holding a key for **2 seconds** accelerates
scrolling, without increasing the size of individual steps:

| View | Initial repeat interval | After holding 2 seconds |
| --- | --- | --- |
| History / Claude wheel | 150 ms | 40 ms |
| Codex fullscreen (three rows per tick) | 250 ms | 75 ms |

These are minimum intervals: actual speed depends on keyboard repeat frequency.
Excess repeats are discarded; no timer generates scrolling after input stops.
Terminals do not report key release, so a gap of 600 ms resets acceleration;
changing direction or scroll handler also resets it and responds immediately.
Repeat state belongs to each pane. The handler batches tmux operations and skips
process inspection for ordinary history. Changes apply on the next keypress;
restarting or reattaching tmx is unnecessary.

For exact one-line scrolling in Codex, launch a new Codex process from a tmx
pane using:

```sh
tmx run codex
# Codex arguments are forwarded, for example:
tmx run codex resume --last
```

This adds `--no-alt-screen -c tui.fullscreen_transcript=false` for that invocation
only. It does not modify saved Codex settings or replace an already running
process. In an existing fullscreen Codex session, the supported minimum wheel
step remains three rows. Plain arrow keys are never injected into the composer.

Verification: actual isolated Codex 0.160.0 sessions confirmed three-row native
wheel steps with mouse reporting off, and one-line tmux scrolling in inline mode.
Actual Muse 1.4.0 one-line scrolling was verified with its local echo provider. Claude is not installed
on this host: its handler is documentation-backed and tested against protocol
fixtures, not qualified against a live Claude process. Customized app bindings,
future versions, and mouse handling inside dialogs may differ.

**Ctrl-i and Tab normally send the same byte**, even with separate physical keys.
Both therefore activate scrolling on legacy terminals. **Ctrl-b then `t`** sends
a literal Tab to the application for completion/navigation. Ctrl-k is also
consumed rather than forwarded to the application. Ctrl-p cycles existing workspaces in the focused side;
Ctrl-o changes which side is focused.

| Keys (press Ctrl-b, release, then the next key) | Action |
| --- | --- |
| `1`–`3` / `4`–`6` | Create/switch workspace on the left / right |
| `o` or Tab | Switch between the two panes |
| `t` | Send a literal Tab to the application |
| `d` | Detach, keeping everything running |
| `u` | Open history; `u`/`d` page and `q` exits |
| `q` / `w` | Send Shift+Left / Shift+Right to the application |
| `r` | Retry opening a shell if automatic reopening failed; never replaces a live process |
| `?` | Show shortcuts |

The **Ctrl-p** and **Ctrl-b then digit** shortcuts use ordinary control and number
characters, so they do not depend on a keyboard supporting Ctrl+number chords. Direct Ctrl+1–6 also remain available for
terminals that send distinct CSI-u or xterm modifyOtherKeys sequences, but are
not required. The wrapper never maps ordinary digits outside the prefix,
Escape, NUL or Backspace to workspace switching.

All split-pane shortcuts, the tmux command prompt, context menus, pane removal
and arbitrary window-creation shortcuts are disabled. Only the launcher creates
background terminals for workspaces. This is a UI constraint, not a security
boundary: the same Unix user can deliberately alter tmux using its socket.
The launcher refuses externally changed visible layouts rather than killing
processes to force compliance.

SSH loss, terminal closure, and client detach leave the session running.
Reattaching takes over from old attached clients. When a workspace's shell exits
(`exit`, Ctrl-d, or a program launched with `exec` ending), it automatically opens
a **fresh shell** in that same pane. Its side and workspace number stay intact,
including when the workspace is in the background. A new shell uses the pane's
starting directory, or your home directory if that directory no longer exists.
It does not rerun the agent or command that exited. Exiting an ordinary program
launched from a shell returns to that existing shell as usual. Exiting tmux with
Ctrl-b then d only detaches; it does not restart the shells.

The new shell starts with a fresh terminal screen; tmux's respawn operation may
reset that workspace's terminal history. Shell variables from an exited process
are gone. Ctrl-b then r is a fallback if automatic reopening fails and never
replaces a live process.
**Host reboot, tmux server termination, or explicit session deletion cannot
preserve live processes.** No reboot restoration is claimed. Terminal resizing
can reflow the display; it does not restart pane processes.

`tmx list` lists left/right workspaces, their pane IDs and which are visible. `tmx history` prints the active pane's retained
history into the calling terminal (detach first to use iSH's native scrollback).
`tmx --detached [1-6]` prepares or switches the numbered workspace on its fixed side without attaching. `tmx` inside this
session is harmless; inside unrelated tmux it asks you to detach first.

The private `tmx` server uses this directory's `tmux.conf`, with a 100,000-line
history limit per pane. Normal `tmux`, `~/.tmux.conf`, shell startup files and
Codex configuration are unchanged. Split panes use tmux history; the old cdx
alternate-screen workaround is not carried over. Native iSH finger scrolling
is not guaranteed for a split tmux display.

Install as dev, without sudo:

```sh
chmod 700 /home/dev/Out/tmx /home/dev/Out/tmx/tmx.sh
ln -s /home/dev/Out/tmx/tmx.sh /home/dev/.local/bin/tmx
```

The former `cdx` command is removed on installation; its source is retained in
`/home/dev/Out/codex-mobile` for reference. No old running sessions are killed.
Configuration changes apply to a newly started server. The independent-workspace
migration reloads the private configuration once, preserving running programs.
Ordinary reattachment does not reload settings or rebuild panes.

Run the isolated integration checks with `python3 -m unittest discover -s tests -v`.
They use private test sockets and never send model requests.

See the [tmux manual](https://man.openbsd.org/tmux) for session persistence,
`remain-on-exit`, and extended key handling.
The wheel encoder follows [xterm mouse reporting](https://invisible-island.net/xterm/ctlseqs/ctlseqs.html#h2-Mouse-Tracking).
Claude's native line actions are listed in its [keyboard reference](https://code.claude.com/docs/en/keybindings#scroll-actions).

Extended-key encoding is documented in the [tmux modifier-key reference](https://github.com/tmux/tmux/wiki/Modifier-Keys).
