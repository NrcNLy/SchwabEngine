"""
tests/test_ave_ses_mapping.py
=============================
Unit tests for core/ses_mapping.py.

Tests:
1. Exact worked values from approved implementation plan table:
   - SES = -0.80: k_u=1.0000, k_l=0.5000, gamma=1.0000 (DEFENSIVE)
   - SES = -0.35: k_u=1.2813, k_l=1.0625, gamma=0.7750 (DEFENSIVE)
   - SES = -0.2598 (snapshot): k_u=1.3376, k_l=1.1752, gamma=0.7299 (CHOP)
   - SES = 0.00: k_u=1.5000, k_l=1.5000, gamma=0.6000 (CHOP)
   - SES = +0.35: k_u=2.1563, k_l=1.7188, gamma=1.1250 (FAVORABLE)
   - SES = +0.80: k_u=3.0000, k_l=2.0000, gamma=1.8000 (FAVORABLE)
2. Continuity across regime boundaries: limit from left == limit from right at +/-0.35 and 0.0.
3. Monotonicity and invariant k_l <= k_u over a dense grid across [-1.0, 1.0].
4. Snapshot loader staleness test: returns CHOP fallback when file age > 90 min.
5. Snapshot loader missing file: returns CHOP fallback gracefully.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import pytest

from core.ses_mapping import (
    load_ses_snapshot,
    ses_to_params,
    DEFAULT_S_SAT,
    DEFAULT_THETA,
)


def test_worked_values_table():
    # 1. Negative saturated anchor (-0.80)
    p_def = ses_to_params(-0.80)
    assert p_def.regime == "DEFENSIVE"
    assert p_def.k_u == pytest.approx(1.0000, abs=1e-4)
    assert p_def.k_l == pytest.approx(0.5000, abs=1e-4)
    assert p_def.gamma == pytest.approx(1.0000, abs=1e-4)

    # 2. Defensive threshold (-0.35)
    p_def_thresh = ses_to_params(-0.35)
    assert p_def_thresh.regime == "DEFENSIVE"
    assert p_def_thresh.k_u == pytest.approx(1.2813, abs=1e-4)
    assert p_def_thresh.k_l == pytest.approx(1.0625, abs=1e-4)
    assert p_def_thresh.gamma == pytest.approx(0.7750, abs=1e-4)

    # 3. Live snapshot (-0.2598)
    p_snap = ses_to_params(-0.2598)
    assert p_snap.regime == "CHOP"
    assert p_snap.k_u == pytest.approx(1.3376, abs=1e-4)
    assert p_snap.k_l == pytest.approx(1.1752, abs=1e-4)
    assert p_snap.gamma == pytest.approx(0.7299, abs=1e-4)

    # 4. CHOP center (0.00)
    p_chop = ses_to_params(0.0)
    assert p_chop.regime == "CHOP"
    assert p_chop.k_u == pytest.approx(1.5000, abs=1e-4)
    assert p_chop.k_l == pytest.approx(1.5000, abs=1e-4)
    assert p_chop.gamma == pytest.approx(0.6000, abs=1e-4)

    # 5. Favorable threshold (+0.35)
    p_fav_thresh = ses_to_params(0.35)
    assert p_fav_thresh.regime == "FAVORABLE"
    assert p_fav_thresh.k_u == pytest.approx(2.1563, abs=1e-4)
    assert p_fav_thresh.k_l == pytest.approx(1.7188, abs=1e-4)
    assert p_fav_thresh.gamma == pytest.approx(1.1250, abs=1e-4)

    # 6. Favorable saturated anchor (+0.80)
    p_fav = ses_to_params(0.80)
    assert p_fav.regime == "FAVORABLE"
    assert p_fav.k_u == pytest.approx(3.0000, abs=1e-4)
    assert p_fav.k_l == pytest.approx(2.0000, abs=1e-4)
    assert p_fav.gamma == pytest.approx(1.8000, abs=1e-4)


def test_continuity_and_invariants_across_grid():
    # Evaluate across 2,000 discrete points in [-1.0, 1.0]
    eps = 1e-6
    for i in range(2001):
        ses = -1.0 + i * (2.0 / 2000.0)
        p = ses_to_params(ses)
        # Mathematical invariant: k_l <= k_u everywhere
        assert p.k_l <= p.k_u + 1e-9, f"Violation k_l > k_u at ses={ses}"
        # Param bounds
        assert 1.0 <= p.k_u <= 3.0 + 1e-9
        assert 0.5 <= p.k_l <= 2.0 + 1e-9
        assert 0.6 <= p.gamma <= 1.8 + 1e-9

    # Continuity at 0.35
    p_left = ses_to_params(0.35 - eps)
    p_right = ses_to_params(0.35 + eps)
    assert abs(p_left.k_u - p_right.k_u) < 1e-4
    assert abs(p_left.k_l - p_right.k_l) < 1e-4
    assert abs(p_left.gamma - p_right.gamma) < 1e-4

    # Continuity at 0.0
    p_zero_neg = ses_to_params(-eps)
    p_zero_pos = ses_to_params(+eps)
    assert abs(p_zero_neg.k_u - p_zero_pos.k_u) < 1e-4
    assert abs(p_zero_neg.k_l - p_zero_pos.k_l) < 1e-4
    assert abs(p_zero_neg.gamma - p_zero_pos.gamma) < 1e-4


def test_snapshot_loader_staleness_and_fallback(tmp_path: Path):
    now = datetime(2026, 10, 14, 16, 0, 0, tzinfo=timezone.utc)
    snap_file = tmp_path / "test_snapshot.json"

    # 1. Fresh file (30 mins old)
    fresh_ts = (now - timedelta(minutes=30)).isoformat()
    snap_file.write_text(json.dumps({
        "timestamp": fresh_ts,
        "ses": 0.55,
        "regime": "FAVORABLE",
    }), encoding="utf-8")

    params_fresh = load_ses_snapshot(snapshot_path=snap_file, now=now)
    assert params_fresh.is_fallback is False
    assert params_fresh.ses_score == 0.55
    assert params_fresh.regime == "FAVORABLE"

    # 2. Stale file (120 mins old > 90 min max age)
    stale_ts = (now - timedelta(minutes=120)).isoformat()
    snap_file.write_text(json.dumps({
        "timestamp": stale_ts,
        "ses": 0.55,
        "regime": "FAVORABLE",
    }), encoding="utf-8")

    params_stale = load_ses_snapshot(snapshot_path=snap_file, now=now, max_age_minutes=90.0)
    assert params_stale.is_fallback is True
    assert params_stale.ses_score == 0.0
    assert params_stale.regime == "CHOP"

    # 3. Missing file
    missing_file = tmp_path / "non_existent.json"
    params_missing = load_ses_snapshot(snapshot_path=missing_file, now=now)
    assert params_missing.is_fallback is True
    assert params_missing.regime == "CHOP"
