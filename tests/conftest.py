from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def library(tmp_path_factory) -> Path:
    """The synthetic fixture library, generated fresh (ffmpeg sine tones tagged with mutagen)."""
    out = tmp_path_factory.mktemp("lib") / "library"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_fixture_library.py"), str(out), "--seconds", "1"],
                   check=True, capture_output=True)
    return out
