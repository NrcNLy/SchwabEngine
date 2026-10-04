"""
core/atomic_io.py
=================
Crash-safe JSON persistence shared by every state store in the engine
(macro_liquidity.json, liquidity_policy.json, nlv_anchor.json, ...).

Guarantees
----------
* Writes go to a temp file in the same directory, are fsync'd, then swapped in
  with ``os.replace`` (atomic on POSIX and NTFS).
* ``os.replace`` is retried with back-off on ``PermissionError`` so a reader
  that briefly holds the file (Windows WinError 32) cannot lose an update.
* ``update_json`` performs read-modify-write under a process-wide re-entrant
  lock so concurrent API threads cannot clobber each other.

All functions here are blocking. Call them from ``asyncio.to_thread`` or from
synchronous FastAPI handlers - never directly from the Tier 1 event loop.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("atomic_io")

STATE_LOCK = threading.RLock()


class StateEncoder(json.JSONEncoder):
    """Serialises Decimal as float and dates as ISO-8601 (what the UI consumes)."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        return super().default(obj)


def read_json(path: Path | str, default: Optional[Any] = None) -> Any:
    """Reads and parses a JSON file; returns ``default`` if missing or corrupt."""
    p = Path(path)
    try:
        with STATE_LOCK:
            if not p.exists():
                return default
            return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("read_json(%s) failed: %s", p, exc)
        return default


def atomic_write_json(
    path: Path | str,
    data: Any,
    max_retries: int = 8,
    retry_delay: float = 0.05,
) -> None:
    """
    Atomically replaces ``path`` with the JSON encoding of ``data``.

    Raises:
        PermissionError: if the swap still fails after ``max_retries`` attempts.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, cls=StateEncoder)

    with STATE_LOCK:
        fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())

            for attempt in range(max_retries):
                try:
                    os.replace(tmp_name, p)
                    return
                except PermissionError as exc:
                    if attempt == max_retries - 1:
                        logger.error("Could not swap %s into %s after %d tries: %s",
                                     tmp_name, p, max_retries, exc)
                        raise
                    logger.warning("File lock on %s (WinError 32). Retry %d/%d",
                                   p, attempt + 1, max_retries)
                    time.sleep(retry_delay * (attempt + 1))
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass


def update_json(
    path: Path | str,
    mutator: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]],
    default: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Read-modify-write under ``STATE_LOCK``.

    ``mutator`` may mutate the dict in place (return ``None``) or return a new
    dict. The resulting document is persisted atomically and returned.
    """
    with STATE_LOCK:
        current = read_json(path, default=None)
        if not isinstance(current, dict):
            current = dict(default or {})
        result = mutator(current)
        if result is not None:
            current = result
        atomic_write_json(path, current)
        return current
