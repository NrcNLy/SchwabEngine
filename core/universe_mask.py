"""
core/universe_mask.py
=====================
Symbol eligibility mask consumed by ``execution.strategies.StrategyEngine``.

A symbol is tradeable only if it is in the configured universe and not on any
exclusion list (cash-sweep funds such as SWVXX, user-configured exclusions,
or active multi-day external swing lots protected against IRC §1091 wash-sale contamination).

Wash-Sale Exclusion Scope:
- Exclusions are STRICTLY triggered by:
  1. Active external swing lots currently held in the account with origination
     timestamps prior to today's open (t_lot < Session_open or external_schwab == True).
  2. Single-session circuit-breaker stops (-$50 risk cap or daily loss limits).
- The engine DOES NOT query trades.db for closed losses within the past 30 days to
  quarantine tickers from new intraday entries; closed intraday losses never prevent
  day-trading the same ticker on subsequent sessions.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any, Dict, Iterable, Optional, Set

logger = logging.getLogger(__name__)


class UniverseExclusionMask:
    def __init__(self, cfg: Dict[str, Any]):
        engine = cfg.get("engine", {}) or {}
        recon = cfg.get("reconciliation", {}) or {}

        self._universe: Set[str] = {str(s).upper() for s in engine.get("symbols", [])}
        self._excluded: Set[str] = {"SWVXX"}
        self._excluded |= {str(s).upper() for s in recon.get("exclude_symbols", [])}
        self._excluded |= {str(s).upper() for s in engine.get("exclude_symbols", [])}

    @property
    def universe(self) -> Set[str]:
        return set(self._universe - self._excluded)

    @property
    def excluded(self) -> Set[str]:
        """Returns the set of currently excluded symbols."""
        return set(self._excluded)

    def exclude(self, symbols: Iterable[str]) -> None:
        self._excluded |= {str(s).upper() for s in symbols}

    def unexclude(self, symbols: Iterable[str]) -> None:
        """Removes symbols from the exclusion mask (e.g. after swing position liquidation)."""
        for s in symbols:
            self._excluded.discard(str(s).upper())

    def is_symbol_tradeable(self, symbol: str) -> bool:
        sym = str(symbol).upper()
        if sym in self._excluded:
            return False
        return not self._universe or sym in self._universe

    def audit_holdings_for_wash_sale(
        self,
        positions: Iterable[Any],
        session_open: Optional[datetime] = None,
    ) -> Set[str]:
        """
        Audit broker portfolio positions to protect active multi-day swing tranches against
        IRC §1091 wash-sale lot contamination.

        Excludes symbols if and only if:
        1. An active lot was acquired prior to today's session open (t_lot < Session_open), OR
        2. The holding is flagged as external_schwab == True (discretionary swing tranche).

        CRITICAL SPECIFICATION:
        Does NOT query trades.db for closed losses from past sessions or past 30 days.
        Closed intraday losses never quarantine tickers from new intraday entries on subsequent sessions.
        """
        newly_excluded: Set[str] = set()
        for pos in positions:
            sym = getattr(pos, "symbol", None) or (pos.get("symbol") if isinstance(pos, dict) else None)
            if not sym:
                continue
            sym = str(sym).upper()

            # Check if external swing or acquired prior to session open
            is_external = bool(
                getattr(pos, "external_schwab", False) or
                (pos.get("external_schwab", False) if isinstance(pos, dict) else False)
            )
            opened_at = getattr(pos, "opened_at", None) or (pos.get("opened_at") if isinstance(pos, dict) else None)

            is_prior_lot = False
            if session_open is not None and opened_at is not None:
                if isinstance(opened_at, (int, float)):
                    is_prior_lot = opened_at < session_open.timestamp()
                elif isinstance(opened_at, datetime):
                    is_prior_lot = opened_at < session_open

            if is_external or is_prior_lot:
                self.exclude([sym])
                newly_excluded.add(sym)
                logger.warning(
                    "UniverseExclusionMask: Symbol %s excluded to protect active external swing lot from IRC §1091 wash-sale contamination.",
                    sym
                )

        return newly_excluded

    def audit_session_circuit_breaker(
        self,
        today_realized_pnl: float,
        daily_loss_limit: float = 50.0,
    ) -> bool:
        """
        Evaluates single-session circuit breaker stop (-$50 risk cap or daily loss limits).
        If tripped, halts trading entries for the rest of the current session.
        """
        if today_realized_pnl <= -abs(daily_loss_limit):
            logger.critical(
                "UniverseExclusionMask: Single-session circuit breaker triggered (PnL -$%.2f <= -$%.2f). Halting new entries.",
                abs(today_realized_pnl), abs(daily_loss_limit)
            )
            return True
        return False

