import json
from pathlib import Path

from tests.golden import build


def test_golden_fixture_is_current():
    """If this fails, regenerate: python -m tests.golden > tests/fixtures/golden.json (and check JS!)."""
    data = json.loads((Path(__file__).parent / "fixtures" / "golden.json").read_text())
    assert data == json.loads(json.dumps(build()))


def test_javascript_parity():
    """Runs the node.js twin tests (skipped if node is not installed)."""
    import glob
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    files = sorted(glob.glob(str(Path(__file__).parent / "js" / "*.test.mjs")))
    p = subprocess.run([node, "--test", *files], capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stdout[-3000:] + p.stderr[-2000:]
