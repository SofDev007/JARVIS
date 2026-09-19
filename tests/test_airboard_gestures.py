"""The browser gesture engine (airboard/static/gestures.js) must reproduce the
original Python engine's scores and decisions, frozen in
tests/data/gesture_golden.json. Runs the JS under Node; skipped without it."""

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_js_gesture_engine_matches_golden():
    script = Path(__file__).parent / "gesture_parity.mjs"
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr or result.stdout
