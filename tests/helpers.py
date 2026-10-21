"""Utilidades de prueba. Los temporales se crean dentro del repositorio (``var/``)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TMP_ROOT = ROOT / "var" / "tmp-tests"


def temp_dir() -> tempfile.TemporaryDirectory[str]:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=TMP_ROOT)
