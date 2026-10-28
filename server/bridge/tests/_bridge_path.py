"""Puts server/bridge on sys.path, so each check also runs on its own: python server/bridge/tests/<check>.py."""

import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
if str(BRIDGE) not in sys.path:
    sys.path.insert(1, str(BRIDGE))
