"""pytest entry point: every check script listed in server/bridge/run_checks.py runs as one test.

The scripts stay standalone (python <script>) so each runs in its own process, as run_checks.py runs them.
"""

import subprocess
import sys
from pathlib import Path

import pytest

CHECK_DIR = Path(__file__).resolve().parents[1] / 'server' / 'bridge'
sys.path.insert(0, str(CHECK_DIR))
from run_checks import CHECKS  # noqa: E402


@pytest.mark.parametrize('script', CHECKS)
def test_check(script):
    proc = subprocess.run(
        [sys.executable, '-u', str(CHECK_DIR / script)], cwd=CHECK_DIR, capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, f'{script} failed\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}'
