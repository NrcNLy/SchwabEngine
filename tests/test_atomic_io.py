from __future__ import annotations

import json
import threading
from datetime import date, datetime
from decimal import Decimal

import pytest

from core import atomic_io
from core.atomic_io import atomic_write_json, read_json, update_json


def test_roundtrip_serialises_decimal_and_dates(tmp_path):
    p = tmp_path / "doc.json"
    atomic_write_json(p, {"amt": Decimal("12.34"), "d": date(2026, 10, 5), "t": datetime(2026, 10, 5, 9, 30)})
    doc = read_json(p)
    assert doc["amt"] == 12.34
    assert doc["d"] == "2026-10-05"
    assert doc["t"].startswith("2026-10-05T09:30")


def test_read_json_returns_default_for_missing_and_corrupt(tmp_path):
    assert read_json(tmp_path / "nope.json", default={"x": 1}) == {"x": 1}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert read_json(bad, default=[]) == []


def test_update_json_is_atomic_under_thread_contention(tmp_path):
    p = tmp_path / "counter.json"

    def bump():
        for _ in range(25):
            update_json(p, lambda d: d.__setitem__("n", d.get("n", 0) + 1))

    threads = [threading.Thread(target=bump) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert read_json(p)["n"] == 8 * 25
    leftovers = [f.name for f in tmp_path.iterdir() if f.name.endswith(".tmp")]
    assert leftovers == []


def test_winerror32_style_lock_is_retried(tmp_path, monkeypatch):
    p = tmp_path / "locked.json"
    real_replace = atomic_io.os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(32, "The process cannot access the file")
        return real_replace(src, dst)

    monkeypatch.setattr(atomic_io.os, "replace", flaky)
    atomic_write_json(p, {"ok": True}, retry_delay=0.0)
    assert calls["n"] == 3
    assert json.loads(p.read_text(encoding="utf-8")) == {"ok": True}


def test_persistent_lock_raises_and_cleans_temp_file(tmp_path, monkeypatch):
    p = tmp_path / "locked.json"

    def always_locked(src, dst):
        raise PermissionError(32, "locked")

    monkeypatch.setattr(atomic_io.os, "replace", always_locked)
    with pytest.raises(PermissionError):
        atomic_write_json(p, {"ok": True}, max_retries=3, retry_delay=0.0)
    assert [f.name for f in tmp_path.iterdir()] == []
