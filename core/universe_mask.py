"""
core/universe_mask.py
=====================
Symbol eligibility mask consumed by ``execution.strategies.StrategyEngine``.

A symbol is tradeable only if it is in the configured universe and not on any
exclusion list (cash-sweep funds such as SWVXX, user-configured exclusions).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Set


class UniverseExclusionMask:
    def __init__(self, cfg: Dict[str, Any]):
        engine = cfg.get("engine", {}) or {}
        recon = cfg.get("reconciliation", {}) or {}
        pm = cfg.get("portfolio_manager", {}) or {}

        self._universe: Set[str] = {str(s).upper() for s in engine.get("symbols", [])}
        self._excluded: Set[str] = {"SWVXX"}
        self._excluded |= {str(s).upper() for s in recon.get("exclude_symbols", [])}
        self._excluded |= {str(s).upper() for s in pm.get("exclude_symbols", [])}

    @property
    def universe(self) -> Set[str]:
        return set(self._universe - self._excluded)

    def exclude(self, symbols: Iterable[str]) -> None:
        self._excluded |= {str(s).upper() for s in symbols}

    def is_symbol_tradeable(self, symbol: str) -> bool:
        sym = str(symbol).upper()
        if sym in self._excluded:
            return False
        return not self._universe or sym in self._universe
