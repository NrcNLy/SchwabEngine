"""
core/paths.py
=============
Single source of truth for where runtime state is persisted.

Everything the engine writes at run time lives under ``STATE_DIR`` (default ``./state``,
override with the ``ENGINE_STATE_DIR`` environment variable). In Docker this directory is a
mounted volume, so liquidity policy, ingested-document state and NLV anchors survive
rebuilds. (It must be a DIRECTORY mount: atomic ``os.replace`` onto a bind-mounted single
file fails with EBUSY.)
"""

from __future__ import annotations

import os
from pathlib import Path

STATE_DIR = Path(os.environ.get("ENGINE_STATE_DIR", "state"))

MACRO_STATE_FILE = STATE_DIR / "macro_liquidity.json"
POLICY_FILE = STATE_DIR / "liquidity_policy.json"
ANCHOR_ACTIVE_FILE = STATE_DIR / "nlv_anchor_active.json"
ANCHOR_SANDBOX_FILE = STATE_DIR / "nlv_anchor_sandbox.json"
DOCUMENTS_DIR = STATE_DIR / "documents"
PROCESSED_HASHES_FILE = DOCUMENTS_DIR / "processed_hashes.json"
