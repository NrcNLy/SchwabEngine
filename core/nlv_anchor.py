"""
core/nlv_anchor.py
==================
Tracks the prior-close NLV so the header can show

    Net Change = NLV - prior_close_NLV      (dollars and percent)

Each ``update`` call records the first NLV seen on a trading day and, once the
session has closed (>= 16:00 ET), records that day's closing NLV. The anchor
for today is the most recent close strictly before today, falling back to the
first NLV seen today (so the number is 0 rather than fabricated on day one).

Caveat (documented in the UI): ACH deposits/withdrawals change NLV without
being P&L; they are not yet filtered out.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime
from decimal import Decimal
from pathlib import Path
from typing import Dict, Optional, Tuple

import pytz

from core.atomic_io import read_json, update_json
from core.liquidity_policy import ZERO, is_business_day, q

logger = logging.getLogger("nlv_anchor")
_EDT = pytz.timezone("America/New_York")
_CLOSE = dtime(16, 0)


class NlvAnchor:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def update(self, nlv: Decimal, now: Optional[datetime] = None) -> None:
        """Record an NLV observation (blocking; call from a thread)."""
        now = now or datetime.now(_EDT)
        day = now.astimezone(_EDT).date().isoformat()
        value = float(q(Decimal(nlv)))
        after_close = is_business_day(now.astimezone(_EDT).date()) and now.astimezone(_EDT).time() >= _CLOSE

        def mutate(doc: Dict) -> None:
            doc.setdefault("first_seen", {}).setdefault(day, value)
            if after_close:
                doc.setdefault("closes", {})[day] = value
            # keep the file small
            for key in ("first_seen", "closes"):
                items = sorted(doc.get(key, {}).items())[-30:]
                doc[key] = dict(items)

        update_json(self.path, mutate, default={"first_seen": {}, "closes": {}})

    def prior_close(self, today: Optional[date] = None) -> Optional[Decimal]:
        today = today or datetime.now(_EDT).date()
        doc = read_json(self.path, default={}) or {}
        closes = {k: v for k, v in doc.get("closes", {}).items() if k < today.isoformat()}
        if closes:
            return q(Decimal(str(closes[max(closes)])))
        first = doc.get("first_seen", {}).get(today.isoformat())
        return q(Decimal(str(first))) if first is not None else None

    def net_change(self, nlv: Decimal, today: Optional[date] = None) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        """Returns (dollars, percent) or (None, None) when no anchor exists yet."""
        anchor = self.prior_close(today)
        if anchor is None or anchor <= 0:
            return None, None
        delta = q(Decimal(nlv) - anchor)
        pct = (delta / anchor * Decimal(100)).quantize(Decimal("0.01"))
        return delta, pct
