"""Available space on /, sampled once a minute by the shared worker."""
import os
from plugins.settings import COLORS


def storage():
    """Percent of total root filesystem space available to this user."""
    try:
        stats = os.statvfs("/")
        if stats.f_blocks <= 0:
            return ""
        percent = min(100, round(100 * max(0, stats.f_bavail) / stats.f_blocks))
        color = COLORS["disk_low"] if percent < 30 else COLORS["disk_medium"] if percent < 50 else COLORS["disk_high"]
        return f"#[fg={COLORS['text']}]sda: #[fg={color}]{percent}%#[fg={COLORS['text']}]"
    except OSError:
        return ""
