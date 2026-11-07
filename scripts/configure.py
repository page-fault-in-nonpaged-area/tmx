#!/usr/bin/env python3
"""Compile YAML to native tmux formats and stdlib-only plugin settings."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import tempfile
import sys

try:
    import yaml
except ImportError:
    sys.exit("tmx configuration requires PyYAML: install requirements-dev.txt or your distribution Python YAML package")

ROOT = Path(__file__).resolve().parents[1]
COLOR_KEYS = {'text', 'background', 'active_border', 'inactive_border', 'divider',
              'pane_border', 'pane_active_border', 'active_x', 'inactive_x',
              'clock_date', 'clock_time', 'disk_low', 'disk_medium', 'disk_high', 'weather_uv', 'weather'}
STATES = {'SUNY', 'CLER', 'PART', 'CLDY', 'OVRC', 'MIST', 'FOGG', 'DRZL', 'RAIN', 'SNOW', 'SLET', 'STRM'}
NAMED = {'default', 'black', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan', 'white'}
NAMED |= {'bright' + name for name in NAMED - {'default'}}


class UniqueLoader(yaml.SafeLoader):
    """Reject accidental duplicate settings instead of silently overriding."""


def unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ValueError('configuration keys must be unique strings')
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def mapping(data, keys, name):
    if not isinstance(data, dict) or set(data) != keys:
        raise ValueError(f'{name}: expected keys {", ".join(sorted(keys))}')


def color(value):
    if not isinstance(value, str):
        raise ValueError('colors must be strings; quote hex colors')
    if value in NAMED or re.fullmatch(r'#[0-9a-fA-F]{6}', value):
        return
    if re.fullmatch(r'colou?r\d{1,3}', value) and int(re.search(r'\d+', value)[0]) <= 255:
        return
    raise ValueError(f'invalid tmux color: {value!r}')


def load(path):
    data = yaml.load(path.read_text(), Loader=UniqueLoader)
    mapping(data, {'colors', 'plugins', 'weather'}, 'config')
    mapping(data['colors'], COLOR_KEYS, 'colors')
    mapping(data['colors']['weather'], STATES, 'weather colors')
    for key, value in data['colors'].items():
        if key == 'weather':
            for value in value.values():
                color(value)
        else:
            color(value)
    mapping(data['plugins'], {'disk', 'weather', 'clock'}, 'plugins')
    if any(type(value) is not bool for value in data['plugins'].values()):
        raise ValueError('plugin switches must be true or false')
    location = data['weather']
    mapping(location, {'city', 'latitude', 'longitude'}, 'weather location')
    if not isinstance(location['city'], str) or not location['city'].strip() or len(location['city']) > 200:
        raise ValueError('invalid weather city')
    for key, limit in (('latitude', 90), ('longitude', 180)):
        value = location[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > limit:
            raise ValueError(f'invalid weather {key}')
        location[key] = float(value)
    return data


def expand(template, data):
    def replace(match):
        value = data
        for key in match[1].split('.'):
            value = value[key]
        return value
    return re.sub(r'\{\{([a-z_.]+)\}\}', replace, template)


def header(data, root):
    colors, enabled = data['colors'], data['plugins']
    text = f'#[fg={colors["text"]}]'
    clock = expand((root / 'plugins/clock.tmux').read_text().strip(), data) if enabled['clock'] else ''

    def published(option):
        if option == '@tmx_weather':
            # tmux 3.3/3.4 mismeasure emoji variation selectors on musl,
            # clipping the right header. Use text presentation there.
            return '#{?#{m/r:^(next-)?3[.][34]([^0-9].*)?$,#{version}},#{s/️//:@tmx_weather},#{@tmx_weather}}'
        return '#{' + option + '}'

    def segment(names):
        # Optional segments recursively join only when their values exist.
        tail, present = '', '0'
        for name in reversed(names):
            if name == 'clock':
                tail, present = '#{T:@tmx_header_clock}', '1'
                continue
            option = '@tmx_storage' if name == 'disk' else '@tmx_weather'
            exists = '#{!=:#{' + option + '},}'
            separator = text + ' | ' if present == '1' else '#{?' + present + ',' + text + ' | ,}'
            tail = '#{?' + exists + ',' + published(option) + separator + ',}' + tail
            present = '1' if present == '1' else exists if present == '0' else '#{||:' + exists + ',' + present + '}'
        if present == '0':
            return ''
        block = ' ' + text + '[ ' + tail + ('' if names and names[-1] == 'clock' else text) + ' ] '
        if enabled['clock']:
            prefixes = ''.join('#{E:@tmx_header_' + name + '}' for name in names if name != 'clock')
            return ' ' + text + '[ ' + prefixes + '#{T:@tmx_header_clock} ] '
        return block if present == '1' else '#{?' + present + ',' + block + ',}'

    full = [name for name in ('disk', 'weather', 'clock') if enabled[name]]
    compact = [name for name in full if name != 'weather']
    clock_only = ['clock'] if enabled['clock'] else []
    lines = [f"set -g @tmx_header_clock '{clock}'"]
    for name, option in (('disk', '@tmx_storage'), ('weather', '@tmx_weather')):
        value = '#{?#{!=:#{' + option + '},},' + published(option) + text + ' | ,}'
        lines.append(f"set -g @tmx_header_{name} '{value}'")
    for name, names in (('full', full), ('compact', compact), ('clock_only', clock_only)):
        lines.append(f"set -g @tmx_header_{name} '{segment(names)}'")
    # Keep the existing width calculation and weather/disk/clock fallback order.
    block = ''
    for name in reversed(('full', 'compact', 'clock_only')):
        option = '@tmx_header_' + name
        fits = '#{e|>=:#{pane_width},#{e|+:4,#{e|+:#{w:#{E:@tmx_header_title}},#{w:#{E:' + option + '}}}}}'
        block = '#{?' + fits + ',#{E:' + option + '},' + block + '}'
    clock_fits = '#{e|>=:#{pane_width},#{e|+:4,#{e|+:#{w:#{E:@tmx_header_title}},#{w:#{E:@tmx_header_clock_only}}}}}'
    ssh_block = '#{?' + clock_fits + ',#{E:@tmx_header_clock_only},}'
    block = '#{?#{E:@tmx_ssh_active},' + ssh_block + ',' + block + '}'
    block = '#{?#{==:#{@tmx_side},right},' + block + ',}'
    lines.append(f"set -g @tmx_header_block '{block}'")
    footer = ''
    for name in reversed(('full', 'compact', 'clock_only')):
        option = '@tmx_header_' + name
        fits = '#{e|>=:#{window_width},#{w:#{E:' + option + '}}}'
        footer = '#{?' + fits + ',#{E:' + option + '},' + footer + '}'
    lines.append(f"set -g @tmx_footer '#[align=centre]{footer}'")
    if enabled['disk'] or enabled['weather']:
        lines.append('set-hook -g client-attached[20] { run-shell -b \'exec python3 "${TMX_LAUNCHER%/*}/plugins/status.py" --watch "$TMX_SOCKET" 2>/dev/null\' }')
    else:
        lines.append('set-hook -gu client-attached[20]')
    lines.append('set-hook -g client-attached[21] { run-shell -b \'exec python3 "${TMX_LAUNCHER%/*}/plugins/ssh.py" --watch "$TMX_SOCKET" 2>/dev/null\' }')
    # Clear any previously published disabled segments on explicit reload.
    for name, option in (('disk', '@tmx_storage'), ('weather', '@tmx_weather')):
        if not enabled[name]:
            lines.append(f"set -g {option} ''")
    return '\n'.join(lines)


def write_changed(path, content):
    if path.exists() and path.read_text() == content:
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as temporary:
        try:
            temporary.write(content)
            temporary.flush()
            os.replace(temporary.name, path)
        finally:
            if os.path.exists(temporary.name):
                os.unlink(temporary.name)


def configure(root=ROOT, config=None, output=None):
    data = load(config or root / 'config.yml')
    data['header_plugins'] = header(data, root)
    compiled = expand((root / 'tmux.conf').read_text(), data)
    del data['header_plugins']
    settings = json.dumps(data, indent=2) + '\n'
    output = output or root / '.generated'
    write_changed(output / 'plugins.json', settings)
    write_changed(output / 'tmux.conf', '# Generated by make configure; edit config.yml instead.\n' + compiled)
    appearance = '\n'.join(line for line in compiled.splitlines() if line.startswith((
        'set -g status', 'set -g @tmx', 'set-hook -g client-attached', 'set-hook -gu client-attached',
        'setw -g pane-border-style', 'setw -g pane-active-border-style'))) + '\n'
    write_changed(output / 'appearance.conf', appearance)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        configure(config=args.config, output=args.output)
    except (ValueError, KeyError, OSError, yaml.YAMLError) as error:
        parser.exit(2, f'tmx configuration: {error}\n')
