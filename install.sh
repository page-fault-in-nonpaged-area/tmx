#!/bin/sh
# Bootstrap the per-user installation. Safe to invoke with curl | sh.
set -eu

main() {
    [ "$(uname -s)" = Linux ] || { printf '%s\n' 'tmx requires Linux.' >&2; return 1; }
    missing=
    for dependency in git tmux bash readlink flock make python3 ps; do
        command -v "$dependency" >/dev/null 2>&1 || missing="$missing $dependency"
    done
    if [ -n "$missing" ]; then
        printf 'tmx: install these dependencies first:%s\n' "$missing" >&2
        if command -v apk >/dev/null 2>&1; then
            printf '%s\n' 'Alpine/iSH: apk add --no-cache curl ca-certificates git tmux bash coreutils util-linux procps make python3 py3-yaml openssh-client' >&2
        fi
        return 1
    fi
    readlink -m -- "$HOME" >/dev/null 2>&1 || {
        printf '%s\n' 'tmx: GNU readlink is required; on Alpine/iSH, install coreutils.' >&2
        return 1
    }
    ps -p "$$" -o pid=,pgid=,tpgid= >/dev/null 2>&1 || {
        printf '%s\n' 'tmx: procps ps is required; on Alpine/iSH, install procps.' >&2
        return 1
    }
    python3 -c 'import yaml; assert (5, 4) <= tuple(map(int, yaml.__version__.split(".")[:2])) < (7, 0)' 2>/dev/null || {
        printf '%s\n' 'tmx: install PyYAML >=5.4,<7 for python3 (Alpine/iSH: apk add py3-yaml; Debian/Ubuntu: apt install python3-yaml).' >&2
        return 1
    }
    version=$(tmux -V)
    python3 -c 'import re, sys; m = re.match(r"tmux (?:next-)?(\d+)\.(\d+)", sys.argv[1]); sys.exit(0 if m and tuple(map(int, m.groups())) >= (3, 3) else 1)' "$version" || {
        printf 'tmx: tmux 3.3 or newer is required for the workspace headers; found %s\n' "$version" >&2
        return 1
    }

    bin_dir=${BIN_DIR:-"$HOME/.local/bin"}
    install_dir=${TMX_INSTALL_DIR:-"${XDG_DATA_HOME:-$HOME/.local/share}/tmx"}
    case $bin_dir in /*) ;; *) printf '%s\n' 'tmx: BIN_DIR must be an absolute path.' >&2; return 1 ;; esac
    case $install_dir in /*) ;; *) printf '%s\n' 'tmx: TMX_INSTALL_DIR must be an absolute path.' >&2; return 1 ;; esac
    repository=https://github.com/page-fault-in-nonpaged-area/tmx.git

    # Reuse a prior checkout installation, without replacing its configuration
    # or pulling over local changes. Reject unrelated commands before cloning.
    if [ -e "$bin_dir/tmx" ] || [ -L "$bin_dir/tmx" ]; then
        [ -L "$bin_dir/tmx" ] || { printf 'tmx: refusing to replace %s/tmx\n' "$bin_dir" >&2; return 1; }
        launcher=$(readlink -f -- "$bin_dir/tmx") || return 1
        [ "${launcher##*/}" = tmx.sh ] || { printf '%s\n' 'tmx: existing symlink is not a tmx checkout.' >&2; return 1; }
        existing_dir=${launcher%/*}
        if [ -n "${TMX_INSTALL_DIR:-}" ] && [ "$(readlink -m -- "$install_dir")" != "$existing_dir" ]; then
            printf '%s\n' 'tmx: existing command points to a different installation.' >&2
            return 1
        fi
        install_dir=$existing_dir
    fi

    if [ -e "$install_dir" ] || [ -L "$install_dir" ]; then
        origin=$(git -C "$install_dir" config --get remote.origin.url 2>/dev/null) || {
            printf 'tmx: refusing to use unrelated directory %s\n' "$install_dir" >&2
            return 1
        }
        checkout_root=$(git -C "$install_dir" rev-parse --show-toplevel 2>/dev/null) || return 1
        [ "$(readlink -f -- "$install_dir")" = "$(readlink -f -- "$checkout_root")" ] || {
            printf 'tmx: directory is not a repository root: %s\n' "$install_dir" >&2
            return 1
        }
        case $origin in
            "$repository"|git@github.com:page-fault-in-nonpaged-area/tmx.git) ;;
            *) printf 'tmx: refusing to use unrelated repository %s\n' "$install_dir" >&2; return 1 ;;
        esac
        [ -f "$install_dir/tmx.sh" ] && [ -f "$install_dir/scripts/install.py" ] || {
            printf 'tmx: incomplete checkout at %s\n' "$install_dir" >&2
            return 1
        }
        printf 'Reusing %s (no git update).\n' "$install_dir"
    else
        mkdir -p -- "$(dirname -- "$install_dir")"
        git clone --depth 1 --branch main "$repository" "$install_dir"
    fi

    make -C "$install_dir" install BIN_DIR="$bin_dir" PYTHON=python3
    printf '%s\n' 'Run tmx to start or reconnect.'
}

# Keep execution after the complete function definition for piped downloads.
main "$@"
