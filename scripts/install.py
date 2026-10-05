#!/usr/bin/env python3
"""Install a per-user symlink, refusing to overwrite unrelated commands."""
import argparse
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def install(bin_dir, source=ROOT / 'tmx.sh'):
    source = source.resolve(strict=True)
    if not os.access(source, os.X_OK):
        raise ValueError(f'launcher is not executable: {source}')
    bin_dir = bin_dir.expanduser().absolute()
    target = bin_dir / 'tmx'
    if os.path.lexists(target):
        if not target.is_symlink() or target.resolve() != source:
            raise ValueError(f'refusing to replace existing command: {target}')
        return target
    bin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    # symlink is atomic and fails if another installer creates this path first.
    target.symlink_to(source)
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir', type=Path, default=Path.home() / '.local/bin')
    args = parser.parse_args()
    try:
        target = install(args.bin_dir)
        print(f'Installed {target} -> {target.resolve()}')
        if target.parent not in [Path(p).absolute() for p in os.get_exec_path()]:
            print(f'Add {target.parent} to your PATH to call tmx from anywhere.')
    except (ValueError, OSError) as error:
        parser.exit(2, f'tmx install: {error}\n')
