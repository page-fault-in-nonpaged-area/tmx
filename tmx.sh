#!/bin/sh
# Private, persistent workspaces inside two visible panes. All launcher operations live here.
set -eu
umask 077

die() { printf 'tmx: %s\n' "$*" >&2; exit 2; }
usage() {
    cat <<'EOF'
Usage: tmx [1-8] | tmx --detached [1-8] | tmx list | tmx history
       tmx app | tmx reset | tmx run codex [Codex arguments]

tmx reconnects to your last workspace and active pane.
Workspaces 1-5 belong to the left; 6-8 belong to the right.

Ctrl-p            next existing workspace in focused side, wrapping around
Ctrl-b then 1-8    create/select an exact workspace on its assigned side
Ctrl-o            next pane, wrapping from right to left
Ctrl-u / Ctrl-k   scroll up / down (hold 2 seconds to accelerate)
Option-i / k      scroll up / down (Escape + i/k)
Option-o / p      next pane / next workspace (Escape + o/p)
Option-r          terminate focused workspace and open a fresh shell
Ctrl-q            send Shift+Left in Codex; ordinary Ctrl-q elsewhere
Ctrl-b then t      send a literal Tab to the application
Ctrl-b then o      switch between left and right panes
Ctrl-b then d      detach; programs keep running
Ctrl-b then u      scroll history; u/d page, q returns
Ctrl-b then q/w    send Shift+Left / Shift+Right to the application
Ctrl-b then r      retry opening a shell if automatic reopening failed
Ctrl-b then ?      show shortcuts

Run any program, including codex, in either shell. No automatic Codex launch.
App-specific scrolling targets Codex, Claude and Muse only. Use 'tmx app' to inspect.
'tmx run codex' uses inline output for exact one-line history scrolling.
An exited workspace automatically opens a fresh shell in the same pane.
Sessions survive terminal/SSH disconnection, not host reboot or tmux shutdown.
EOF
}

case ${1-} in -h|--help|help) usage; exit 0 ;; esac
command -v tmux >/dev/null 2>&1 || die 'tmux is required'
command -v flock >/dev/null 2>&1 || die 'flock is required'
TMX_LAUNCHER=$(readlink -f -- "$0")
directory=${TMX_LAUNCHER%/*}
TMX_SOCKET=${TMX_SOCKET:-tmx}
case $TMX_SOCKET in tmx|tmx-test-*) ;; *) die 'unsupported TMX_SOCKET' ;; esac
case $TMX_SOCKET in *[!a-zA-Z0-9_-]*) die 'invalid socket name' ;; esac
export TMX_LAUNCHER TMX_SOCKET
mux() { command tmux -L "$TMX_SOCKET" -f "$directory/.generated/tmux.conf" "$@" 9>&-; }

# Cache each side's small tab list. tmux expands the active-workspace condition
# itself during redraws, so no shell process is needed to render the top bar.
refresh_tabs() {
    tab_rows=$(mux list-panes -s -t '=tmx' -F '#{@tmx_side} #{@tmx_workspace}' | sort -k1,1 -k2,2n)
    left_tabs=
    right_tabs=
    while read -r tab_side tab_number; do
        case $tab_number in [1-9]) ;; *) continue ;; esac
        tab_label=$tab_number
        # Right-hand labels are local 1-3; comparisons retain fixed IDs 6-8.
        if [ "$tab_side" = right ]; then tab_label=$((tab_number - 5)); fi
        tab=$(printf '[#{?#{==:#{@tmx_workspace},%s},#[fg=#{?pane_active,#{@tmx_color_active_x},#{@tmx_color_inactive_x}}]●#[fg=#{@tmx_color_text}],%s}]' "$tab_number" "$tab_label")
        case $tab_side in
            left) left_tabs="${left_tabs:+$left_tabs }$tab" ;;
            right) right_tabs="${right_tabs:+$right_tabs }$tab" ;;
        esac
    done <<EOF
$tab_rows
EOF
    mux set-option -t '=tmx:' @tmx_left_tabs "$left_tabs" \; \
        set-option -t '=tmx:' @tmx_right_tabs "$right_tabs"
}

agent_name() {
    case $1 in
        codex|codex-cli) printf 'codex\n' ;;
        claude|claude-code) printf 'claude\n' ;;
        muse|muse-code|muse-bin-*) printf 'muse\n' ;;
        *) return 1 ;;
    esac
}

detect_agent() {
    agent_pane=$1
    if [ "$#" -eq 3 ]; then
        foreground=$2
        agent_tty=$3
    else
        agent_details=$(mux display-message -p -t "$agent_pane" '#{pane_tty} #{pane_current_command}')
        read -r agent_tty foreground <<EOF
$agent_details
EOF
    fi
    if agent_name "$foreground"; then return; fi
    # A Node/Bun launcher may conceal the agent's name. Inspect only the terminal's
    # foreground process group, not arbitrary descendants or prompt arguments.
    agent_pids=$(ps -t "${agent_tty#/dev/}" -o pid=,pgid=,tpgid= 2>/dev/null |
        awk '$2 == $3 && $3 > 0 {print $1}')
    for agent_pid in $agent_pids; do
        agent_exe=$(readlink "/proc/$agent_pid/exe" 2>/dev/null || :)
        if agent_name "${agent_exe##*/}"; then return; fi
        case ${agent_exe##*/} in node|nodejs|bun) ;; *) continue ;; esac
        agent_script=$(tr '\000' '\n' < "/proc/$agent_pid/cmdline" 2>/dev/null | sed -n '2p')
        case $agent_script in
            */@openai/codex/bin/codex.js) printf 'codex\n'; return ;;
            */@anthropic-ai/claude-code/cli.js) printf 'claude\n'; return ;;
        esac
    done
    printf 'other\n'
}

# Resolve current visible focus at dispatch time; queued workspace swaps may
# have parked the key binding's original pane context already.
if [ "${1-}" = question-key ]; then
    [ "$#" -eq 1 ] || die 'question-key takes no arguments'
    main=$(mux show-option -qv -t '=tmx:' @tmx_main_window)
    [ -n "$main" ] || exit 0
    pane=$(mux display-message -p -t "$main" '#{pane_id}')
    if [ "$(detect_agent "$pane")" = codex ]; then
        mux send-keys -t "$pane" S-Left
    else
        mux send-keys -t "$pane" -H 11
    fi
    exit 0
fi

if [ "${1-}" = app ]; then
    [ "$#" -eq 1 ] || die 'app takes no arguments; it inspects the active pane'
    detect_agent '=tmx:'
    exit 0
fi
if [ "${1-}" = run ]; then
    [ "${2-}" = codex ] || die 'run currently supports codex only'
    [ -n "${TMUX-}" ] || die 'run this command in a tmx pane'
    own_socket=$(mux display-message -p '#{socket_path}' 2>/dev/null || :)
    [ "${TMUX%%,*}" = "$own_socket" ] || die 'run this command in a tmx pane'
    shift 2
    # Invocation-only settings; normal codex and its saved configuration stay intact.
    exec codex --no-alt-screen -c tui.fullscreen_transcript=false "$@"
fi

# Invoked by pane-died, including for workspaces parked in background windows.
# Always open a shell explicitly: bare respawn-pane would rerun the last agent
# or command. Never use -k, so a late/duplicate hook cannot kill a live process.
if [ "${1-}" = reopen ]; then
    [ "$#" -eq 2 ] || die 'reopen requires a pane ID'
    pane=$2
    case $pane in %*) ;; *) die 'invalid pane ID' ;; esac
    case ${pane#%} in ''|*[!0-9]*) die 'invalid pane ID' ;; esac
    details=$(mux display-message -p -t "$pane" '#{session_name} #{pane_dead}' 2>/dev/null || :)
    [ "$details" = 'tmx 1' ] || exit 0
    start_dir=$(mux display-message -p -t "$pane" '#{pane_start_path}')
    [ -d "$start_dir" ] || start_dir=$HOME
    mux respawn-pane -t "$pane" -c "$start_dir" /bin/bash -l \; \
        set-option -pu -t "$pane" @tmx_scroll_state
    exit 0
fi

# Explicitly reset only the workspace visible and focused at dispatch time.
# Share the workspace lock so an earlier queued swap cannot redirect the reset
# to a parked terminal. The automatic reopen hook never kills a live process.
if [ "${1-}" = reset ]; then
    [ "$#" -eq 1 ] || die 'reset takes no arguments; it resets the focused workspace'
    state=${XDG_STATE_HOME:-$HOME/.local/state}/tmx
    mkdir -p "$state"
    exec 9>"$state/$TMX_SOCKET.lock"
    flock -x 9
    main=$(mux show-option -qv -t '=tmx:' @tmx_main_window 2>/dev/null || :)
    [ -n "$main" ] || exit 0
    details=$(mux display-message -p -t "$main" '#{session_name} #{window_id} #{pane_id} #{@tmx_side} #{@tmx_workspace}
#{pane_current_path}')
    header=${details%%'
'*}
    start_dir=${details#*'
'}
    read -r session window pane owner number <<EOF
$header
EOF
    [ "$session" = tmx ] && [ "$window" = "$main" ] || die 'cannot resolve visible workspace'
    case $owner:$number in left:[1-5]|right:[6-8]) ;; *) die 'invalid workspace ownership; leaving terminal untouched' ;; esac
    registered=$(mux show-option -qv -t '=tmx:' "@tmx_workspace_$number")
    [ "$registered" = "$pane" ] || die 'workspace registry mismatch; leaving terminal untouched'
    [ -d "$start_dir" ] || start_dir=$HOME
    mux respawn-pane -k -t "$pane" -c "$start_dir" /bin/bash -l \; \
        clear-history -t "$pane" \; \
        set-option -pu -t "$pane" @tmx_scroll_state
    exit 0
fi

# Scroll routing runs before session creation/locking. The binding captures the
# pane ID so queued repeats cannot drift into another pane after a focus change.
if [ "${1-}" = scroll ]; then
    [ "$#" -eq 3 ] || die 'scroll requires up/down and a pane ID'
    direction=$2
    pane=$3
    case $direction in up) button=64 ;; down) button=65 ;; *) die 'invalid scroll direction' ;; esac
    case $pane in %*) ;; *) die 'invalid pane ID' ;; esac
    case ${pane#%} in ''|*[!0-9]*) die 'invalid pane ID' ;; esac
    # One snapshot supplies routing and repeat state; no extra tmux query for
    # app detection on each tick. Foreground command is last (it may have spaces).
    details=$(mux display-message -p -t "$pane" '#{pane_in_mode} #{mouse_any_flag} #{mouse_sgr_flag} #{alternate_on} #{pane_width} #{pane_height} #{?@tmx_scroll_state,#{@tmx_scroll_state},0 0 0 none none} #{pane_tty} #{pane_current_command}')
    [ -n "$details" ] || exit 0
    read -r in_mode mouse sgr alternate width height emitted last_input began last_direction last_route pane_tty foreground <<EOF
$details
EOF
    interval=150
    fast_interval=40
    route=history
    agent=other
    # Plain inline output and existing copy mode need no process inspection.
    if [ "$in_mode" = 0 ] && { [ "$alternate" = 1 ] || [ "$mouse" = 1 ]; }; then
        agent=$(detect_agent "$pane" "$foreground" "$pane_tty")
        case $agent in
            codex)
                # Codex 0.160 fullscreen accepts SGR wheels even with tmux mouse
                # reporting off. Its main view scrolls three rows per wheel tick.
                if [ "$alternate" = 1 ]; then
                    route=codex-wheel; interval=250; fast_interval=75
                fi ;;
            claude)
                if [ "$mouse" = 1 ]; then route=wheel
                elif [ "$alternate" = 1 ]; then route=unsupported; fi ;;
            muse)
                # Muse 1.4 uses inline terminal history. No guessed fullscreen keys.
                if [ "$alternate" = 1 ]; then route=unsupported; fi ;;
            *) if [ "$alternate" = 1 ]; then route=unsupported; fi ;;
        esac
    fi
    if [ "$route" = unsupported ]; then
        mux display-message -t "$pane" "tmx: no verified line-scroll handler for $agent in this view; Ctrl-b u opens terminal history"
        exit 0
    fi
    # No scroll at the ordinary live bottom, and no repeat state to carry over.
    if [ "$direction" = down ] && [ "$in_mode" = 0 ] && [ "$route" = history ]; then
        mux set-option -pu -t "$pane" @tmx_scroll_state
        exit 0
    fi
    # Linux uptime is monotonic. Track input separately from emitted ticks so
    # discarded repeats still count towards a two-second hold. Terminals give
    # no key-up event: a 600 ms gap, direction or route change starts a new hold.
    read -r tmx_uptime tmx_idle < /proc/uptime
    now=$(( ${tmx_uptime%.*} * 1000 + (1${tmx_uptime#*.} - 100) * 10 ))
    gap=$((now - last_input))
    if [ "$last_direction" != "$direction" ] || [ "$last_route" != "$route" ] ||
       [ "$gap" -lt 0 ] || [ "$gap" -ge 600 ]; then
        began=$now
        emitted=0
    fi
    if [ "$((now - began))" -ge 2000 ]; then interval=$fast_interval; fi
    elapsed=$((now - emitted))
    if [ "$emitted" -ne 0 ] && [ "$elapsed" -ge 0 ] && [ "$elapsed" -lt "$interval" ]; then
        mux set-option -p -t "$pane" @tmx_scroll_state "$emitted $now $began $direction $route"
        exit 0
    fi
    # Batch state and delivery into a single tmux call. Never sleep or schedule
    # future ticks; releasing the key cannot leave a timer scrolling on its own.
    set -- set-option -p -t "$pane" @tmx_scroll_state "$now $now $began $direction $route"
    if [ "$in_mode" = 1 ]; then
        set -- "$@" \; send-keys -t "$pane" -X -N 1 "scroll-$direction"
        # Evaluate the bottom check after scrolling, in the same command queue.
        if [ "$direction" = down ]; then
            set -- "$@" \; if-shell -F -t "$pane" '#{==:#{scroll_position},0}' "send-keys -t $pane -X cancel"
        fi
    elif [ "$route" = wheel ] || [ "$route" = codex-wheel ]; then
        # A wheel event over the pane's center lets mouse-aware TUIs scroll their
        # own transcript, including content that never entered tmux history.
        column=$(( (width + 1) / 2 ))
        row=$(( (height + 1) / 2 ))
        if [ "$sgr" = 1 ] || [ "$route" = codex-wheel ]; then
            packet=$(printf '\033[<%s;%s;%sM' "$button" "$column" "$row")
            set -- "$@" \; send-keys -t "$pane" -l "$packet"
        else
            # ASCII coordinates work in both legacy X10 and UTF-8 mouse mode.
            [ "$column" -le 95 ] || column=95
            [ "$row" -le 95 ] || row=95
            set -- "$@" \; send-keys -t "$pane" -H 1b 5b 4d \
                "$(printf '%x' "$((button + 32))")" \
                "$(printf '%x' "$((column + 32))")" \
                "$(printf '%x' "$((row + 32))")"
        fi
    elif [ "$direction" = up ]; then
        set -- "$@" \; copy-mode -e -t "$pane" \; send-keys -t "$pane" -X -N 1 scroll-up
    fi
    mux "$@"
    # Down at the bottom of ordinary terminal history is deliberately a no-op.
    exit 0
fi

[ -r "$directory/.generated/tmux.conf" ] || die "run make configure in $directory first"

action=attach
number=
side=
case ${1-} in
    --detached) action=detached; shift ;;
    workspace) action=workspace; shift ;; # Called by tmux key bindings.
    list|history) action=$1; shift ;;
esac
if [ "$#" -gt 0 ]; then
    case $1 in
        [1-8]) number=$1 ;;
        next) [ "$action" = workspace ] || die 'next is only valid for workspace cycling'; number=next ;;
        *) die 'expected a workspace number from 1 to 8; see tmx --help' ;;
    esac
    shift
fi
if [ "$action" = workspace ] && [ "$#" -gt 0 ]; then
    case $1 in
        left|right) side=$1 ;;
        # Accept old bindings during hot reload, but do not trust their context.
        %*) case ${1#%} in ''|*[!0-9]*) die 'invalid pane ID' ;; esac ;;
        *) die 'expected left, right or a pane ID' ;;
    esac
    shift
fi
[ "$#" -eq 0 ] || die 'too many arguments'
case $action in
    list) [ -z "$number" ] || die 'list takes no workspace number'
          mux list-panes -s -t '=tmx' -F '#{@tmx_side} #{@tmx_workspace} #{pane_id}#{?#{==:#{window_id},#{@tmx_main_window}}, [visible],}' | sort; exit ;;
    history) [ -z "$number" ] || die 'history prints the last active pane; takes no number'
             mux capture-pane -p -J -S - -t '=tmx:'; exit ;;
    workspace) [ -n "$number" ] || die 'workspace needs a number' ;;
esac

# Refuse to nest under unrelated tmux. Within tmx, change the existing session.
inside=no
if [ -n "${TMUX-}" ]; then
    actual=${TMUX%%,*}
    expected=$(mux display-message -p '#{socket_path}' 2>/dev/null || :)
    [ -n "$expected" ] && [ "$actual" = "$expected" ] || die 'detach from the other tmux first (Ctrl-b then d)'
    inside=yes
fi
if [ "$action" = attach ] && [ "$inside" = no ]; then
    [ -t 0 ] && [ -t 1 ] || die 'a terminal is required; use ssh -t or tmx --detached'
    case ${TERM:-dumb} in dumb|unknown|'') die 'connect from a terminal with TERM set' ;; esac
fi

# Serialize first launch and concurrent workspace shortcuts. The server and pane
# children never inherit this descriptor, so a detached session cannot hold it.
state=${XDG_STATE_HOME:-$HOME/.local/state}/tmx
mkdir -p "$state"
exec 9>"$state/$TMX_SOCKET.lock"
flock -x 9

fresh=no
if ! mux has-session -t '=tmx' 2>/dev/null; then
    mux new-session -d -s tmx -n view -c "$PWD"
    mux split-window -d -h -t '=tmx:1' -c "$PWD"
    fresh=yes
fi

# Migrate old whole-window workspaces in place. Tag each existing terminal with
# its side and workspace number; keep the active window as the permanent view.
# Nothing is killed, respawned or swapped during migration. Validate everything
# first so externally changed layouts are left untouched.
model=$(mux show-option -qv -t '=tmx:' @tmx_workspace_model)
if [ "$model" != 2 ] && [ "$model" != 3 ] && [ "$model" != 4 ]; then
    windows=$(mux list-windows -t '=tmx' -F '#{window_index} #{window_panes}')
    while read -r index count; do
        case $index in [1-9]) ;; *) die 'cannot migrate a workspace outside 1-9' ;; esac
        [ "$count" = 2 ] || die 'cannot migrate a workspace without exactly two panes; leaving it untouched'
    done <<EOF
$windows
EOF
    main=$(mux display-message -p -t '=tmx:' '#{window_id}')
    panes=$(mux list-panes -s -t '=tmx' -F '#{pane_id} #{window_index} #{pane_index}')
    while read -r tagged_pane index position; do
        case $position in 1) tagged_side=left ;; 2) tagged_side=right ;; *) die 'unexpected pane index' ;; esac
        if [ "$fresh" = yes ] && [ "$tagged_side" = left ]; then
            case $number in [1-5]) index=$number ;; *) index=1 ;; esac
        fi
        mux set-option -p -t "$tagged_pane" @tmx_side "$tagged_side" \; \
            set-option -p -t "$tagged_pane" @tmx_workspace "$index"
    done <<EOF
$panes
EOF
    mux set-option -t '=tmx:' @tmx_main_window "$main" \; \
        source-file "$directory/.generated/tmux.conf" \; \
        set-option -t '=tmx:' @tmx_workspace_model 2
fi
# Model 4 expands the left to 1-5 and moves the right to 6-8. Preserve sparse
# model-3 IDs by offsetting only the right; older overlapping labels are ordered.
# Validate every record before relabelling in one tmux command queue. Existing
# terminals, processes, positions and focus survive without creating shells.
model=$(mux show-option -qv -t '=tmx:' @tmx_workspace_model)
if [ "$model" != 4 ]; then
    rows=$(mux list-panes -s -t '=tmx' -F '#{pane_id} #{@tmx_side} #{@tmx_workspace}' | sort -k2,2 -k3,3n)
    left_count=0
    right_count=0
    seen_left=' '
    seen_right=' '
    set -- set-option -t '=tmx:' @tmx_workspace_model 4
    for slot in 1 2 3 4 5 6 7 8; do
        set -- "$@" \; set-option -qu -t '=tmx:' "@tmx_workspace_$slot"
    done
    while read -r stored_pane stored_side stored_number; do
        case $stored_number in [1-9]) ;; *) die 'invalid legacy workspace number' ;; esac
        case $stored_side in
            left)
                case $seen_left in *" $stored_number "*) die 'duplicate left workspace' ;; esac
                seen_left="$seen_left$stored_number "
                left_count=$((left_count + 1)); slot=$left_count
                [ "$left_count" -le 5 ] || die 'too many left workspaces to migrate safely' ;;
            right)
                case $seen_right in *" $stored_number "*) die 'duplicate right workspace' ;; esac
                seen_right="$seen_right$stored_number "
                right_count=$((right_count + 1)); slot=$((right_count + 5))
                [ "$right_count" -le 3 ] || die 'too many right workspaces to migrate safely' ;;
            *) die 'workspace has no side; leaving terminals untouched' ;;
        esac
        if [ "$model" = 3 ]; then
            case $stored_side:$stored_number in
                left:[1-3]) slot=$stored_number ;;
                right:[4-6]) slot=$((stored_number + 2)) ;;
                *) die 'invalid model-3 workspace; leaving terminals untouched' ;;
            esac
            registered=$(mux show-option -qv -t '=tmx:' "@tmx_workspace_$stored_number")
            [ "$registered" = "$stored_pane" ] || die 'workspace registry mismatch; leaving terminals untouched'
        fi
        # A fresh launch can start left workspace 1-5 without an extra shell.
        if [ "$fresh" = yes ] && [ "$stored_side" = left ]; then slot=$stored_number; fi
        set -- "$@" \; set-option -t '=tmx:' "@tmx_workspace_$slot" "$stored_pane" \; \
            set-option -p -t "$stored_pane" @tmx_workspace "$slot"
    done <<EOF
$rows
EOF
    [ "$left_count" -ge 1 ] && [ "$right_count" -ge 1 ] || die 'missing workspace side'
    mux "$@"
fi
main=$(mux show-option -v -t '=tmx:' @tmx_main_window)
[ "$(mux display-message -p -t "$main" '#{window_panes}')" = 2 ] ||
    die 'visible layout was modified outside tmx; leaving it untouched'

if [ -n "$number" ]; then
    # The registry is authoritative. Pane-local labels are for display only.
    candidates=
    registry=$(mux display-message -p -t "$main" '1 #{@tmx_workspace_1}
2 #{@tmx_workspace_2}
3 #{@tmx_workspace_3}
4 #{@tmx_workspace_4}
5 #{@tmx_workspace_5}
6 #{@tmx_workspace_6}
7 #{@tmx_workspace_7}
8 #{@tmx_workspace_8}')
    pane_rows=$(mux list-panes -s -t '=tmx' -F '#{pane_id} #{@tmx_side} #{@tmx_workspace}')
    while read -r slot registered; do
        [ -n "$registered" ] || continue
        case $slot in [1-5]) owner=left ;; *) owner=right ;; esac
        valid=no
        while read -r row; do
            if [ "$row" = "$registered $owner $slot" ]; then valid=yes; break; fi
        done <<EOF
$pane_rows
EOF
        [ "$valid" = yes ] || die 'workspace registry mismatch; leaving terminals untouched'
        candidates="${candidates}${candidates:+
}$registered $owner $slot"
    done <<EOF
$registry
EOF
    # Resolve the visible slots by geometry, then verify their registered owner.
    visible=$(mux list-panes -t "$main" -F '#{pane_left} #{pane_id}' | sort -n)
    left_pane=
    right_pane=
    while read -r column visible_pane; do
        if [ -z "$left_pane" ]; then left_pane=$visible_pane; else right_pane=$visible_pane; fi
    done <<EOF
$visible
EOF
    left_number=
    right_number=
    while read -r candidate candidate_side candidate_number; do
        if [ "$candidate" = "$left_pane" ] && [ "$candidate_side" = left ]; then left_number=$candidate_number; fi
        if [ "$candidate" = "$right_pane" ] && [ "$candidate_side" = right ]; then right_number=$candidate_number; fi
    done <<EOF
$candidates
EOF
    [ -n "$left_number" ] && [ -n "$right_number" ] || die 'visible terminals violate fixed workspace sides; leaving them untouched'
    case $number in
        [1-5]) side=left ;;
        [6-8]) side=right ;;
        next)
            # Never trust the key queue's old pane context: a swap may have
            # parked it already. Query focus in the permanent view under lock.
            if [ -z "$side" ]; then
                focused=$(mux display-message -p -t "$main" '#{pane_id}')
                case $focused in
                    "$left_pane") side=left ;;
                    "$right_pane") side=right ;;
                    *) die 'cannot resolve visible focus' ;;
                esac
            fi ;;
    esac
    case $side in
        left) current=$left_pane; active_number=$left_number ;;
        right) current=$right_pane; active_number=$right_number ;;
        *) die 'cannot determine focused side' ;;
    esac
    if [ "$number" = next ]; then
        # Choose under the workspace lock so repeated presses cannot race.
        # Only existing workspaces on this side participate; gaps are skipped.
        first_number=10
        next_number=10
        while read -r candidate candidate_side candidate_number; do
            [ "$candidate_side" = "$side" ] || continue
            case $candidate_number in [1-9]) ;; *) die 'invalid stored workspace number' ;; esac
            if [ "$candidate_number" -lt "$first_number" ]; then first_number=$candidate_number; fi
            if [ "$candidate_number" -gt "$active_number" ] && [ "$candidate_number" -lt "$next_number" ]; then
                next_number=$candidate_number
            fi
        done <<EOF
$candidates
EOF
        [ "$first_number" -lt 10 ] || die 'no workspaces found for this side'
        if [ "$next_number" -lt 10 ]; then number=$next_number; else number=$first_number; fi
    fi
    desired=
    side_count=0
    while read -r candidate candidate_side candidate_number; do
        [ "$candidate_side" = "$side" ] || continue
        side_count=$((side_count + 1))
        if [ "$candidate_number" = "$number" ]; then
            [ -z "$desired" ] || die 'duplicate workspace; leaving terminals untouched'
            desired=$candidate
        fi
    done <<EOF
$candidates
EOF
    if [ -z "$desired" ]; then
        # Count under the same lock as creation, so simultaneous requests cannot
        # exceed the cap. Existing workspaces always remain selectable.
        if [ "$side" = left ]; then side_limit=5; else side_limit=3; fi
        if [ "$side_count" -ge "$side_limit" ]; then
            message="$side already has $side_limit workspaces; Ctrl-p cycles existing workspaces"
            if [ "$action" = workspace ]; then
                mux display-message -t "$current" "tmx: $message"
                exit 0
            fi
            die "$message"
        fi
        start_dir=$(mux display-message -p -t "$current" '#{pane_current_path}')
        [ -d "$start_dir" ] || start_dir=$PWD
        # Background windows store inactive terminals. Only the two panes in the
        # permanent view are ever shown. Switching swaps terminals, not windows.
        desired=$(mux new-window -d -P -F '#{pane_id}' -t '=tmx:' -n stored -c "$start_dir")
        mux set-option -p -t "$desired" @tmx_side "$side" \; \
            set-option -p -t "$desired" @tmx_workspace "$number" \; \
            set-option -t '=tmx:' "@tmx_workspace_$number" "$desired"
        # Keep the parked terminal at the visible slot's dimensions. Otherwise
        # swapping into a full-width background window reflows history and can
        # move copy mode away from the line the user was reading.
        dimensions=$(mux display-message -p -t "$current" '#{pane_width} #{pane_height}')
        read -r parked_width parked_height <<EOF
$dimensions
EOF
        mux set-option -w -t "$desired" window-size manual \; \
            set-option -w -t "$desired" pane-border-status off \; \
            resize-window -t "$desired" -x "$parked_width" -y "$parked_height"
    fi
    if [ "$current" != "$desired" ]; then
        mux swap-pane -d -s "$desired" -t "$current" \; select-pane -t "$desired"
    else
        mux select-pane -t "$desired"
    fi
fi
refresh_tabs
# Reconnect to the permanent view without changing its focused side.
mux select-window -t "$main"
flock -u 9
exec 9>&-

if [ "$action" = attach ] && [ "$inside" = no ]; then
    # Move the session to this client; do not leave a stale phone client in charge.
    exec tmux -L "$TMX_SOCKET" -f "$directory/.generated/tmux.conf" attach-session -d -t '=tmx'
fi
