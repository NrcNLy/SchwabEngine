"""
Shared pytest fixtures.

State is redirected to a throw-away directory BEFORE any engine module is imported,
because ``core.paths`` resolves ``ENGINE_STATE_DIR`` at import time. Tests therefore can
never touch real policy / document / anchor files.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_STATE_TMP = tempfile.mkdtemp(prefix="engine_test_state_")
os.environ["ENGINE_STATE_DIR"] = _STATE_TMP
os.environ.pop("DOC_PARSER_SAMPLE_FALLBACK", None)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state_dir():
    """Each test starts with an empty state directory."""
    base = Path(_STATE_TMP)
    for child in list(base.rglob("*")):
        if child.is_file():
            try:
                child.unlink()
            except OSError:
                pass
    yield
