"""Configuration loader.

Policies, agents, source types and matching thresholds live in JSON files in this
folder, so they can be reviewed and changed without editing code. Set
LEDGERSIGHT_CONFIG_DIR to load a different folder (e.g. per deployment).
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("LEDGERSIGHT_CONFIG_DIR") or Path(__file__).resolve().parent)


@lru_cache(maxsize=None)
def load(name: str) -> dict:
    return json.loads((CONFIG_DIR / name).read_text(encoding="utf-8"))
