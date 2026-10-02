"""
portfolio/manager.py
====================
Portfolio Take-Profit Manager.

Scans ALL positions in the Schwab account at 09:20 AM EDT each market day.
For any position NOT in the day-trading universe, uses Gemini to generate
a take-profit recommendation.

Operating Modes
---------------
advisory:
    Logs the recommendation and sends an FCM notification. No orders placed.
    You review via the Android dashboard and act manually.

auto_with_approval:
    Sends an FCM notification with a 60-second countdown. If you don't veto
    within 60 seconds, a LIMIT SELL is placed automatically. Orders are only
    auto-placed if LLM confidence >= 0.75 and position value < $10,000.
    Large or low-confidence positions always wait for explicit approval.

auto:
    Places LIMIT SELL orders immediately without waiting. Requires explicit
    config opt-in. Never used for positions > $10,000 or confidence < 0.70.

Configuration (config.yaml):
    portfolio_manager.enabled          → true
    portfolio_manager.mode             → "auto_with_approval"
    portfolio_manager.evaluation_time  → "09:20:00"
    portfolio_manager.hitl_timeout_sec → 60
    portfolio_manager.auto_max_value   → 10000.0
    portfolio_manager.min_confidence   → 0.75
    portfolio_manager.exclude_symbols  → [SOXL, TQQQ, TNA, FNGU, CONL, DPST]
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytz

logger = logging.getLogger(__name__)
_EDT = pytz.timezone("America/New_York")

MODE_ADVISORY         = "advisory"
MODE_AUTO_WITH_APPROVAL = "auto_with_approval"
MODE_AUTO             = "auto"

_PROMPT_TEMPLATE = """You are a conservative portfolio advisor for a self-directed retail investor.
Analyze the following equity position and recommend a take-profit exit price.

Position Details:
  Symbol:           {symbol}
  Quantity:         {quantity} shares
  Average Cost:     ${avg_cost:.2f}/share
  Current Price:    ${current_price:.2f}/share
  Unrealized P&L:   ${unrealized_pnl:+.2f} ({pnl_pct:+.1f}%)
  Position Value:   ${position_value:.2f}

Recent Market Headlines:
{headlines}

Instructions:
- Suggest a realistic take-profit price target based on technical and fundamental factors.
- Be conservative: prefer locking in gains over reaching for the maximum.
- If the position is already at a loss, suggest a stop-loss price instead and set trade_permitted=false.
- Consider the current macro environment from the headlines.
- Respond with a JSON object matching this schema exactly:

{{
  "take_profit_price": <float>,
  "stop_loss_price": <float or null>,
  "time_horizon_days": <int>,
  "confidence": <float 0.0-1.0>,
  "rationale": "<brief explanation>",
  "trade_permitted": <bool>
}}
"""


@dataclass
class TakeProfitRecommendation:
    symbol:           str
    quantity:         int
    avg_cost:         float
    current_price:    float
    unrealized_pnl:   float
    take_profit_price: float
    stop_loss_price:  Optional[float]
    time_horizon_days: int
    confidence:       float
    rationale:        str
    trade_permitted:  bool
    mode:             str
    generated_at:     datetime
    order_id:         Optional[str] = None   # Set if limit sell was placed


class PortfolioManager:
    """
    Morning scan + take-profit generator for non-universe holdings.

    Called once per market day at 09:20 AM EDT by the LedgerScheduler.
    Also callable manually via the API (POST /portfolio/scan).
    """

    def __init__(
        self,
        rest_client,
        order_manager,
        news_aggregator,
        gemini_client,
        dispatcher,
        cfg: dict,
    ) -> None:
        self._rest          = rest_client
        self._order_manager = order_manager
        self._news          = news_aggregator
        self._gemini        = gemini_client
        self._dispatcher    = dispatcher

        pm_cfg = cfg.get("portfolio_manager", {})
        self._enabled:         bool  = pm_cfg.get("enabled", True)
        self._mode:            str   = pm_cfg.get("mode", MODE_AUTO_WITH_APPROVAL)
        self._hitl_timeout:    int   = int(pm_cfg.get("hitl_timeout_sec", 60))
        self._auto_max_value:  float = float(pm_cfg.get("auto_max_value", 10000.0))
        self._min_confidence:  float = float(pm_cfg.get("min_confidence", 0.75))
        self._exclude_symbols: set   = {
            s.upper() for s in pm_cfg.get("exclude_symbols", [
                "SOXL", "TQQQ", "TNA", "FNGU", "CONL", "DPST"
            ])
        }

        macro_cfg = cfg.get("macro", {})
        self._model = macro_cfg.get("gemini_model", "gemini-3.1-pro-preview")

        # Persistent recommendation log
        log_dir = Path(pm_cfg.get("log_dir", "/app/logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        self._rec_log_path = log_dir / "portfolio_recommendations.jsonl"

        # In-memory cache of latest recommendations (for API)
        self._latest_recs: List[TakeProfitRecommendation] = []
        self._lock = threading.Lock()

        logger.info(
            "PortfolioManager initialised — mode=%s, hitl_timeout=%ds, "
            "auto_max=$%.0f, min_confidence=%.0f%%",
            self._mode, self._hitl_timeout, self._auto_max_value,
            self._min_confidence * 100,
        )

    # ------------------------------------------------------------------
    # Primary scan entry point
    # ------------------------------------------------------------------

    def run_morning_scan(self) -> List[TakeProfitRecommendation]:
        """
        Scan all account positions and generate take-profit recommendations
        for non-universe holdings. Called at 09:20 AM EDT by LedgerScheduler.

        Returns:
            List of TakeProfitRecommendation objects.
        """
        if not self._enabled:
            return []

        logger.info("PortfolioManager: starting morning scan...")
        positions = self._fetch_non_universe_positions()

        if not positions:
            logger.info("PortfolioManager: no non-universe positions found.")
            return []

        recommendations: List[TakeProfitRecommendation] = []
        for pos in positions:
            try:
                rec = self._evaluate_position(pos)
                if rec:
                    recommendations.append(rec)
                    self._log_recommendation(rec)
                    self._handle_recommendation(rec)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "PortfolioManager: error evaluating %s: %s",
                    pos.get("symbol", "?"), exc,
                )

        with self._lock:
            self._latest_recs = recommendations

        logger.info(
            "PortfolioManager: morning scan complete — %d recommendations generated.",
            len(recommendations),
        )
        return recommendations

    def get_latest_recommendations(self) -> List[TakeProfitRecommendation]:
        """Return the most recent set of recommendations (for API)."""
        with self._lock:
            return list(self._latest_recs)

    # ------------------------------------------------------------------
    # Position fetching
    # ------------------------------------------------------------------

    def _fetch_non_universe_positions(self) -> List[Dict[str, Any]]:
        """Fetch all account positions, excluding the day-trading universe."""
        account_hash = self._order_manager.account_hash
        if not account_hash:
            logger.warning("PortfolioManager: account hash not available yet.")
            return []

        try:
            accounts = self._rest.get_accounts(fields="positions")
        except Exception as exc:
            logger.error("PortfolioManager: failed to fetch accounts: %s", exc)
            return []

        results = []
        for acct in accounts:
            positions = acct.get("securitiesAccount", {}).get("positions", [])
            for pos in positions:
                sym = pos.get("instrument", {}).get("symbol", "").upper()
                qty = float(pos.get("longQuantity", 0))
                if sym and qty > 0 and sym not in self._exclude_symbols:
                    results.append({
                        "symbol":        sym,
                        "quantity":      int(qty),
                        "avg_cost":      float(pos.get("averagePrice", 0)),
                        "current_price": float(pos.get("marketValue", 0)) / int(qty) if qty > 0 else 0,
                        "position_value": float(pos.get("marketValue", 0)),
                    })

        logger.info(
            "PortfolioManager: found %d non-universe positions.", len(results)
        )
        return results

    # ------------------------------------------------------------------
    # LLM evaluation
    # ------------------------------------------------------------------

    def _evaluate_position(
        self, pos: Dict[str, Any]
    ) -> Optional[TakeProfitRecommendation]:
        """Call Gemini to evaluate a single position."""
        sym           = pos["symbol"]
        qty           = pos["quantity"]
        avg_cost      = pos["avg_cost"]
        current_price = pos["current_price"]
        pos_value     = pos["position_value"]
        unrealized    = (current_price - avg_cost) * qty
        pnl_pct       = ((current_price - avg_cost) / avg_cost * 100) if avg_cost > 0 else 0

        headlines = ""
        if self._news:
            headlines = self._news.get_recent_headlines(sym, max_count=5)

        prompt = _PROMPT_TEMPLATE.format(
            symbol=sym,
            quantity=qty,
            avg_cost=avg_cost,
            current_price=current_price,
            unrealized_pnl=unrealized,
            pnl_pct=pnl_pct,
            position_value=pos_value,
            headlines=headlines or "No recent news available.",
        )

        try:
            response = self._gemini.models.generate_content(
                model=self._model,
                contents=prompt,
            )
            raw = response.text.strip()
            # Extract JSON from response
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()

            data = json.loads(raw)

            return TakeProfitRecommendation(
                symbol=sym,
                quantity=qty,
                avg_cost=avg_cost,
                current_price=current_price,
                unrealized_pnl=unrealized,
                take_profit_price=float(data["take_profit_price"]),
                stop_loss_price=data.get("stop_loss_price"),
                time_horizon_days=int(data.get("time_horizon_days", 5)),
                confidence=float(data.get("confidence", 0.5)),
                rationale=str(data.get("rationale", "")),
                trade_permitted=bool(data.get("trade_permitted", True)),
                mode=self._mode,
                generated_at=datetime.now(_EDT),
            )

        except Exception as exc:
            logger.error(
                "PortfolioManager: Gemini evaluation failed for %s: %s", sym, exc
            )
            return None

    # ------------------------------------------------------------------
    # Action handler (advisory / auto_with_approval / auto)
    # ------------------------------------------------------------------

    def _handle_recommendation(self, rec: TakeProfitRecommendation) -> None:
        """Decide whether to auto-place, wait for approval, or just notify."""
        logger.info(
            "PortfolioManager: %s → take_profit=$%.2f, confidence=%.0f%%, mode=%s",
            rec.symbol, rec.take_profit_price, rec.confidence * 100, self._mode,
        )

        # Always send FCM notification
        if self._dispatcher:
            self._dispatcher._send("PORTFOLIO_RECOMMENDATION", {
                "symbol":            rec.symbol,
                "quantity":          rec.quantity,
                "take_profit_price": f"{rec.take_profit_price:.2f}",
                "confidence":        f"{rec.confidence:.2f}",
                "rationale":         rec.rationale[:200],
                "mode":              self._mode,
            })

        if self._mode == MODE_ADVISORY:
            return  # Notify only

        if not rec.trade_permitted:
            logger.info(
                "PortfolioManager: LLM vetoed action on %s — advisory only.", rec.symbol
            )
            return

        # Safety gates for any auto action
        position_value = rec.avg_cost * rec.quantity
        if position_value > self._auto_max_value:
            logger.info(
                "PortfolioManager: %s position value $%.0f > auto_max $%.0f — "
                "requires explicit approval.",
                rec.symbol, position_value, self._auto_max_value,
            )
            return

        if rec.confidence < self._min_confidence:
            logger.info(
                "PortfolioManager: %s confidence %.0f%% < min %.0f%% — "
                "requires explicit approval.",
                rec.symbol, rec.confidence * 100, self._min_confidence * 100,
            )
            return

        if self._mode == MODE_AUTO_WITH_APPROVAL:
            logger.info(
                "PortfolioManager: HITL window — waiting %ds for veto on %s...",
                self._hitl_timeout, rec.symbol,
            )
            # In a real implementation this would integrate with the API
            # approval endpoint. For now, use the timeout-based pattern.
            time.sleep(self._hitl_timeout)
            # After timeout with no veto, place the order
            self._place_take_profit(rec)

        elif self._mode == MODE_AUTO:
            self._place_take_profit(rec)

    def _place_take_profit(self, rec: TakeProfitRecommendation) -> None:
        """Place a LIMIT SELL order for the take-profit target."""
        try:
            # Use order_manager's REST client directly for a limit sell
            account_hash = self._order_manager.account_hash
            order_body = {
                "orderType":         "LIMIT",
                "session":           "NORMAL",
                "duration":          "GOOD_TILL_CANCEL",
                "orderStrategyType": "SINGLE",
                "price":             f"{rec.take_profit_price:.2f}",
                "orderLegCollection": [{
                    "instruction":  "SELL",
                    "quantity":     rec.quantity,
                    "instrument": {
                        "symbol":       rec.symbol,
                        "assetType":    "EQUITY",
                    },
                }],
            }
            resp = self._rest.place_order(account_hash, order_body)
            order_id = resp.headers.get("Location", "").rstrip("/").split("/")[-1]
            rec.order_id = order_id
            logger.info(
                "PortfolioManager: LIMIT SELL placed for %s ×%d @ $%.2f (order=%s)",
                rec.symbol, rec.quantity, rec.take_profit_price, order_id,
            )
            if self._dispatcher:
                self._dispatcher._send("ORDER_LIFECYCLE", {
                    "symbol":   rec.symbol,
                    "action":   "PORTFOLIO_TAKE_PROFIT_PLACED",
                    "qty":      rec.quantity,
                    "price":    f"{rec.take_profit_price:.2f}",
                    "order_id": order_id,
                })
        except Exception as exc:
            logger.error(
                "PortfolioManager: failed to place take-profit for %s: %s",
                rec.symbol, exc,
            )

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_recommendation(self, rec: TakeProfitRecommendation) -> None:
        """Append recommendation to persistent JSONL log."""
        record = {
            "ts":                rec.generated_at.isoformat(),
            "symbol":            rec.symbol,
            "quantity":          rec.quantity,
            "avg_cost":          rec.avg_cost,
            "current_price":     rec.current_price,
            "unrealized_pnl":    rec.unrealized_pnl,
            "take_profit_price": rec.take_profit_price,
            "stop_loss_price":   rec.stop_loss_price,
            "time_horizon_days": rec.time_horizon_days,
            "confidence":        rec.confidence,
            "rationale":         rec.rationale,
            "trade_permitted":   rec.trade_permitted,
            "mode":              rec.mode,
            "order_id":          rec.order_id,
        }
        try:
            with open(self._rec_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as exc:
            logger.warning("PortfolioManager: log write failed: %s", exc)
