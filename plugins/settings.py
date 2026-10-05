"""Read build-generated settings once per worker, never per redraw or key."""
import json
from pathlib import Path

SETTINGS = json.loads((Path(__file__).resolve().parents[1] / ".generated/plugins.json").read_text())
COLORS = SETTINGS["colors"]
ENABLED = SETTINGS["plugins"]
