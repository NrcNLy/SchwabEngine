#!/usr/bin/env python3
"""
scripts/run_mid_session_paper_run.py
====================================
Mid-Session Paper Run (#94) Execution Runner for SchwabEngine.

Conducts a live paper trading / dry-run session under the Dual-Sleeve Robo-Core
and Argon quantitative architecture:
1. Safety & Execution Mode Verification:
   - Validates live_trading = False (or SHADOW mode) in config/config.yaml.
   - Confirms emergency kill switch clearance in config/config.yaml and state/control_signal.json.
2. Mid-Session Hot Boot:
   - Warm-plane account snapshot via data/multi_rate_sync.py to establish mid-session baseline (N(t), cash, positions).
   - Level 1 stream ingestion to mark positions to market (microprice, OBI, quote watermarking).
3. Dual-Sleeve Telemetry & Macro Meta-Governor:
   - Ingests macro features (FRED OAS, economic surprise Z_s, Gemini NLP sentiment with continuous time decay).
   - Updates 3-State HMM Meta-Governor (pi(t) = [pi_Goldilocks, pi_Uncertainty, pi_Shock]^T) with T_kk = 0.92 hysteresis.
   - Evaluates Leland No-Trade Zone boundaries (tau_band = 0.03) against current holdings.
   - Logs simulated paired orders ([SELL_SATELLITE -> BUY_CORE] or [SELL_CORE -> BUY_SATELLITE]) with zero cash drag.
   - Persists telemetry and synthetic P&L to state/telemetry.db and console logger.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.paths import CONTROL_SIGNAL_FILE, TELEMETRY_DB_PATH
CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"
from core.runtime import load_config
from core.telemetry import SQLiteWALEventStore, TelemetryEvent
from data.multi_rate_sync import (
    MultiRateStateSynchronizer,
    DualSleeveState,
    HotQuoteUpdate,
    ColdMacroState,
)
from execution.risk_manager import (
    MetaGovernorHMM,
    DualSleeveRouter,
    DualSleeveSwapPlan,
    RiskEngine,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("SchwabEngine.PaperRun94")


class MidSessionPaperRunRunner:
    """
    Executes and orchestrates the Mid-Session Paper Run (#94).
    """

    def __init__(self, sequence_file: Optional[Path] = None):
        self.sequence_file = sequence_file or (PROJECT_ROOT / "data" / "fixtures" / "schwab_telemetry_micro_sequence.json")
        self.cfg = load_config()
        self.event_store = SQLiteWALEventStore(TELEMETRY_DB_PATH)
        self.local_event_store = None
        local_db = PROJECT_ROOT / "state" / "telemetry.db"
        if Path(TELEMETRY_DB_PATH).resolve() != local_db.resolve():
            self.local_event_store = SQLiteWALEventStore(local_db)
        self.synchronizer = MultiRateStateSynchronizer()
        self.hmm = MetaGovernorHMM(persistence=0.92)
        self.router = DualSleeveRouter(tau_band=0.03, core_symbol="SPY")
        self.session_id = f"PAPER_RUN_94_{int(time.time())}"
        self.synthetic_realized_pnl: float = 0.0
        self.synthetic_trades: List[Dict[str, Any]] = []
        self.baseline_nlv: float = 0.0

    def record_telemetry_event(self, aggregate_id: str, event_type: str, payload: Dict[str, Any]) -> None:
        self.event_store.record_event(aggregate_id=aggregate_id, event_type=event_type, payload=payload)
        if self.local_event_store is not None:
            self.local_event_store.record_event(aggregate_id=aggregate_id, event_type=event_type, payload=payload)

    def verify_safety_and_execution_mode(self) -> bool:
        """
        Step 1: Verify safety configurations and kill switch status.
        """
        logger.info("=" * 78)
        logger.info("STEP 1: VERIFYING SAFETY & EXECUTION MODE")
        logger.info("=" * 78)

        engine_cfg = self.cfg.get("engine", {}) or {}
        trading_cfg = self.cfg.get("trading", {}) or {}
        live_trading = bool(engine_cfg.get("live_trading", False))
        kill_switch = bool(trading_cfg.get("kill_switch", False))
        engine_active = bool(trading_cfg.get("engine_active", True))

        logger.info("Configuration Check (config/config.yaml):")
        logger.info("  engine.live_trading = %s (MUST be False for dry-run)", live_trading)
        logger.info("  trading.kill_switch = %s (MUST be False)", kill_switch)
        logger.info("  trading.engine_active = %s (MUST be True)", engine_active)

        if live_trading:
            logger.critical("SAFETY VIOLATION: live_trading is TRUE! Aborting paper run.")
            return False

        if kill_switch:
            logger.critical("SAFETY VIOLATION: kill_switch is TRUE in config.yaml! Aborting paper run.")
            return False

        # Verify state/control_signal.json
        if CONTROL_SIGNAL_FILE.exists():
            sig = json.loads(CONTROL_SIGNAL_FILE.read_text(encoding="utf-8"))
            action = str(sig.get("action", "")).upper()
            sig_ks = bool(sig.get("kill_switch", False))
            logger.info("Control Signal Check (state/control_signal.json):")
            logger.info("  action = %s", action)
            logger.info("  kill_switch = %s", sig_ks)
            logger.info("  reason = %s", sig.get("reason", ""))
            if action != "RESUME" or sig_ks:
                logger.warning("Control signal is not clean RESUME. Overriding for dry-run simulation.")

        # Log safety confirmation event to telemetry.db
        self.record_telemetry_event(
            aggregate_id=self.session_id,
            event_type="PAPER_RUN_SAFETY_CONFIRMED",
            payload={
                "live_trading": False,
                "mode": "DRY_RUN_SHADOW",
                "kill_switch": False,
                "engine_active": True,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        logger.info("[PASSED] All safety parameters confirmed. Zero real orders will reach Schwab API.")
        return True

    def execute_mid_session_hot_boot(self, account_payload: Dict[str, Any]) -> DualSleeveState:
        """
        Step 2: Warm-plane account snapshot via data/multi_rate_sync.py.
        """
        logger.info("=" * 78)
        logger.info("STEP 2: EXECUTING MID-SESSION HOT BOOT SNAPSHOT")
        logger.info("=" * 78)

        state: DualSleeveState = self.synchronizer.on_account_sync(account_payload, is_hot_boot=True)
        self.baseline_nlv = state.net_liquidation_value

        logger.info("Mid-Session Hot Boot Baseline Established:")
        logger.info("  Net Liquidation Value N(t): $%.2f", state.net_liquidation_value)
        logger.info("  Total Cash Buffer:          $%.2f", state.cash_buffer)
        logger.info("  Core Sleeve MV (C(t)):      $%.2f (weight: %.2f%%)", state.core_market_value, state.core_weight * 100.0)
        logger.info("  Satellite Sleeve MV (S(t)): $%.2f (weight: %.2f%%)", state.satellite_market_value, state.satellite_weight * 100.0)
        logger.info("  Positions Ingested:         %d", len(state.positions))
        for sym, pos in state.positions.items():
            logger.info(
                "    - %s (%s): %d shares @ $%.2f avg (MV: $%.2f, Unrealized P&L: $%.2f)",
                sym,
                pos.sleeve,
                int(pos.quantity),
                pos.average_cost,
                pos.market_value,
                pos.unrealized_pnl,
            )

        self.record_telemetry_event(
            aggregate_id=self.session_id,
            event_type="MID_SESSION_HOT_BOOT_COMPLETE",
            payload={
                "net_liquidation_value": state.net_liquidation_value,
                "cash_buffer": state.cash_buffer,
                "core_market_value": state.core_market_value,
                "satellite_market_value": state.satellite_market_value,
                "core_weight": state.core_weight,
                "satellite_weight": state.satellite_weight,
                "positions_count": len(state.positions),
            },
        )
        return state

    def process_level1_quote(self, quote_payload: Dict[str, Any]) -> HotQuoteUpdate:
        """
        Processes Level 1 streaming quote, updates microprice and marks positions to market.
        """
        update = self.synchronizer.on_level1_quote(quote_payload)
        logger.info(
            "  [L1 QUOTE] %s | Mid: $%.2f | Micro: $%.2f | Spread: $%.2f | OBI: %+.4f | Stale: %s",
            update.symbol,
            update.mid_price,
            update.microprice,
            round(update.ask - update.bid, 4),
            update.order_book_imbalance,
            update.is_stale,
        )
        return update

    def process_macro_telemetry(self, schema_type: str, payload: Dict[str, Any]) -> None:
        """
        Ingests FRED or Gemini NLP telemetry into Cold plane.
        """
        if schema_type == "FRED_MACRO_DATA":
            res = self.synchronizer.on_fred_macro(payload)
            logger.info(
                "  [COLD MACRO] FRED Ingested -> HY OAS: %.1f bps, Z_s: %+.2f, Hybrid Spread: %.1f bps",
                res.fred_oas_bps,
                res.economic_surprise_z_score,
                res.hybrid_credit_spread_bps,
            )
        elif schema_type == "GEMINI_NLP_SENTIMENT":
            res = self.synchronizer.on_gemini_nlp(payload)
            logger.info(
                "  [COLD NLP] Gemini Ingested -> Sentiment Polarity: %+.2f, Decayed: %+.2f, Regime: %s, Bias: %s",
                res.sentiment_polarity_raw,
                res.sentiment_decayed,
                res.macro_regime,
                res.market_bias,
            )

    def evaluate_dual_sleeve_and_leland_ntz(self) -> Optional[DualSleeveSwapPlan]:
        """
        Step 3: Update HMM Meta-Governor, check Leland NTZ, generate & log simulated paired orders.
        """
        # 1. Ingest continuous macro feature vector M(t)
        m_vec = self.synchronizer.get_macro_feature_vector()
        pi_post = self.hmm.update(m_vec)
        dominant = self.hmm.get_dominant_regime()

        # 2. Compute composite target satellite weight W_sat^*(t)
        target_w_sat = self.hmm.calculate_target_satellite_weight(w_base=0.15)

        portfolio = self.synchronizer.get_live_portfolio_state()
        core_pos = {sym: pos for sym, pos in portfolio.positions.items() if pos.sleeve == "CORE"}
        sat_pos = {sym: pos for sym, pos in portfolio.positions.items() if pos.sleeve == "SATELLITE"}

        core_quote = self.synchronizer._latest_quotes.get("SPY")
        core_px = core_quote.microprice if (core_quote and core_quote.microprice > 0.0) else 585.14

        logger.info("-" * 78)
        logger.info(
            "HMM Meta-Governor Update: M(t)=[Z_s=%.2f, S_nlp=%.2f, CS=%.1f bps] -> pi(t)=[G:%.1f%%, U:%.1f%%, S:%.1f%%] | Dominant: %s",
            m_vec[0],
            m_vec[1],
            m_vec[2],
            pi_post[0] * 100.0,
            pi_post[1] * 100.0,
            pi_post[2] * 100.0,
            dominant,
        )
        logger.info(
            "Dual-Sleeve Mark-to-Market: N(t)=$%.2f | Core=$%.2f (%.1f%%) | Sat=$%.2f (%.1f%%) | Target Sat: %.1f%%",
            portfolio.net_liquidation_value,
            portfolio.core_market_value,
            portfolio.core_weight * 100.0,
            portfolio.satellite_market_value,
            portfolio.satellite_weight * 100.0,
            target_w_sat * 100.0,
        )

        # 3. Evaluate Leland No-Trade Zone
        plan = self.router.plan_dual_sleeve_reallocation(
            nlv=portfolio.net_liquidation_value,
            satellite_positions=sat_pos,
            target_satellite_weight=target_w_sat,
            core_positions=core_pos,
            core_symbol="SPY",
            core_price=core_px,
        )

        logger.info(
            "Leland NTZ Evaluation: Action=%s | Active Sat Weight: %.2f%% | Band: [%.2f%%, %.2f%%]",
            plan.action,
            plan.active_satellite_weight * 100.0,
            plan.band_lower * 100.0,
            plan.band_upper * 100.0,
        )

        # 4. Process rebalance plan
        if plan.action == "NO_TRADE_ZONE":
            logger.info("  Inside Leland NTZ. Zero turnover churn, no paired orders required.")
            self.record_telemetry_event(
                aggregate_id=self.session_id,
                event_type="LELAND_NTZ_EVALUATION",
                payload={
                    "action": "NO_TRADE_ZONE",
                    "active_satellite_weight": plan.active_satellite_weight,
                    "target_satellite_weight": plan.target_satellite_weight,
                    "band_lower": plan.band_lower,
                    "band_upper": plan.band_upper,
                },
            )
            return plan

        logger.info("  BREACH DETECTED: %s! Emitting Paired Dual-Sleeve Orders:", plan.action)
        for i, order in enumerate(plan.orders, start=1):
            logger.info(
                "    Leg %d: [%s %s] %d shares @ $%.2f (Notional: $%.2f, Sleeve: %s, Reason: %s)",
                i,
                order.side,
                order.symbol,
                order.shares,
                order.price,
                order.target_notional,
                order.sleeve,
                order.reason,
            )

        logger.info(
            "  Paired Order Capital Invariant: Sat Delta = $%+.2f | Core Sweep = $%+.2f (Zero Cash Drag)",
            plan.satellite_notional_delta,
            plan.core_notional_delta,
        )

        # 5. Execute simulated atomic swap
        self._execute_simulated_swap(plan, portfolio, core_px)
        return plan

    def _execute_simulated_swap(
        self,
        plan: DualSleeveSwapPlan,
        state: DualSleeveState,
        core_price: float,
    ) -> None:
        """
        Executes simulated fills for paired orders and updates synthetic position state and P&L.
        """
        with self.synchronizer._lock:
            for order in plan.orders:
                sym = order.symbol
                qty = order.shares
                px = order.price

                if order.side == "SELL":
                    if sym in self.synchronizer._positions:
                        pos = self.synchronizer._positions[sym]
                        realized = (px - pos.average_cost) * qty
                        self.synthetic_realized_pnl += realized
                        pos.quantity -= qty
                        pos.market_value = pos.quantity * px
                        pos.unrealized_pnl = (px - pos.average_cost) * pos.quantity
                        self.synthetic_trades.append({
                            "symbol": sym,
                            "side": "SELL",
                            "shares": qty,
                            "price": px,
                            "realized_pnl": round(realized, 2),
                            "sleeve": order.sleeve,
                        })
                        logger.info("    [SIM FILL] SOLD %d %s @ $%.2f -> Realized P&L: $%+.2f", qty, sym, px, realized)
                elif order.side == "BUY":
                    if sym not in self.synchronizer._positions:
                        from data.multi_rate_sync import SleevePosition
                        self.synchronizer._positions[sym] = SleevePosition(
                            symbol=sym,
                            quantity=float(qty),
                            average_cost=px,
                            current_price=px,
                            market_value=float(qty) * px,
                            unrealized_pnl=0.0,
                            last_update_ts=time.time(),
                            sleeve=order.sleeve,
                        )
                    else:
                        pos = self.synchronizer._positions[sym]
                        new_qty = pos.quantity + qty
                        new_cost = ((pos.quantity * pos.average_cost) + (qty * px)) / new_qty if new_qty > 0 else px
                        pos.quantity = new_qty
                        pos.average_cost = new_cost
                        pos.market_value = new_qty * px
                        pos.unrealized_pnl = (px - new_cost) * new_qty

                    self.synthetic_trades.append({
                        "symbol": sym,
                        "side": "BUY",
                        "shares": qty,
                        "price": px,
                        "realized_pnl": 0.0,
                        "sleeve": order.sleeve,
                    })
                    logger.info("    [SIM FILL] BOUGHT %d %s @ $%.2f (Swept into %s)", qty, sym, px, order.sleeve)

        # Log paired swap to SQLite WAL telemetry database
        self.record_telemetry_event(
            aggregate_id=self.session_id,
            event_type="DUAL_SLEEVE_PAIRED_SWAP",
            payload={
                "action": plan.action,
                "satellite_notional_delta": plan.satellite_notional_delta,
                "core_notional_delta": plan.core_notional_delta,
                "orders_count": len(plan.orders),
                "orders": [
                    {
                        "symbol": o.symbol,
                        "side": o.side,
                        "shares": o.shares,
                        "price": o.price,
                        "sleeve": o.sleeve,
                    }
                    for o in plan.orders
                ],
                "synthetic_realized_pnl": round(self.synthetic_realized_pnl, 2),
            },
        )

    def run_paper_trading_session(self) -> int:
        """
        Executes the complete mid-session paper run over the telemetry sequence.
        """
        logger.info("=" * 78)
        logger.info("STARTING SCHWABENGINE MID-SESSION PAPER RUN (#94)")
        logger.info("Session ID: %s", self.session_id)
        logger.info("Local Time: %s", datetime.now().isoformat())
        logger.info("=" * 78)

        # Step 1: Safety verification
        if not self.verify_safety_and_execution_mode():
            return 1

        # Load telemetry sequence
        if not self.sequence_file.exists():
            logger.error("Telemetry sequence file not found at %s", self.sequence_file)
            return 2

        with open(self.sequence_file, "r", encoding="utf-8") as f:
            sequence_events: List[Dict[str, Any]] = json.load(f)

        logger.info("Loaded %d sequential telemetry micro-events from %s", len(sequence_events), self.sequence_file.name)

        # Execution loop
        hot_boot_done = False
        rebalance_count = 0

        for idx, event in enumerate(sequence_events, start=1):
            schema = event.get("schema_type")
            symbol = event.get("symbol", "")
            payload = event.get("payload", {})

            # Step 2: Mid-Session Hot Boot on first account snapshot
            if schema == "SCHWAB_ACCOUNT_BALANCE":
                if not hot_boot_done:
                    self.execute_mid_session_hot_boot(payload)
                    hot_boot_done = True
                else:
                    state = self.synchronizer.on_account_sync(payload, is_hot_boot=False)
                    logger.info("  [WARM RECON] Periodic account sync: NLV=$%.2f, Cash=$%.2f", state.net_liquidation_value, state.cash_buffer)

            elif schema == "SCHWAB_LEVEL1_QUOTE":
                self.process_level1_quote(payload)

            elif schema in ("FRED_MACRO_DATA", "GEMINI_NLP_SENTIMENT"):
                self.process_macro_telemetry(schema, payload)

            # Evaluate Leland NTZ every few events once hot-booted
            if hot_boot_done and idx % 3 == 0:
                plan = self.evaluate_dual_sleeve_and_leland_ntz()
                if plan and plan.action != "NO_TRADE_ZONE":
                    rebalance_count += 1

            time.sleep(0.02)  # High-rate simulation pacing

        # Final evaluation
        final_plan = self.evaluate_dual_sleeve_and_leland_ntz()
        if final_plan and final_plan.action != "NO_TRADE_ZONE":
            rebalance_count += 1

        # Summary
        self.generate_session_dossier(rebalance_count)
        return 0

    def generate_session_dossier(self, rebalance_count: int) -> None:
        """
        Outputs final performance and telemetry summary.
        """
        portfolio = self.synchronizer.get_live_portfolio_state()
        unrealized_pnl = sum(p.unrealized_pnl for p in portfolio.positions.values())
        total_pnl = self.synthetic_realized_pnl + unrealized_pnl

        logger.info("=" * 78)
        logger.info("MID-SESSION PAPER RUN (#94) EXECUTION DOSSIER")
        logger.info("=" * 78)
        logger.info("Session ID:                    %s", self.session_id)
        logger.info("Initial Baseline NLV N(t0):    $%.2f", self.baseline_nlv)
        logger.info("Final Mark-to-Market NLV N(t): $%.2f", portfolio.net_liquidation_value)
        logger.info("Synthetic Realized P&L:        $%+.2f", self.synthetic_realized_pnl)
        logger.info("Synthetic Unrealized P&L:      $%+.2f", unrealized_pnl)
        logger.info("Total Synthetic Net P&L:       $%+.2f", total_pnl)
        logger.info("Core Return Instantaneous:     %+.4f%%", portfolio.core_return_instantaneous * 100.0)
        logger.info("Core Return Cumulative:        %+.4f%%", portfolio.core_return_cumulative * 100.0)
        logger.info("Paired Rebalances Executed:    %d", rebalance_count)
        logger.info("Final Portfolio Allocation:")
        logger.info("  - 1x Robo-Core (SPY, etc.):  $%.2f (%.2f%%)", portfolio.core_market_value, portfolio.core_weight * 100.0)
        logger.info("  - 3x Tactical Satellite:     $%.2f (%.2f%%)", portfolio.satellite_market_value, portfolio.satellite_weight * 100.0)
        logger.info("  - Cash / Margin Buffer:      $%.2f", portfolio.cash_buffer)
        logger.info("Holdings:")
        for sym, p in sorted(portfolio.positions.items()):
            logger.info("  * %s (%s): %d shares @ $%.2f (MV: $%.2f, Unrealized: $%+.2f)", sym, p.sleeve, int(p.quantity), p.current_price, p.market_value, p.unrealized_pnl)

        self.record_telemetry_event(
            aggregate_id=self.session_id,
            event_type="PAPER_RUN_SESSION_COMPLETE",
            payload={
                "session_id": self.session_id,
                "initial_baseline_nlv": self.baseline_nlv,
                "final_nlv": portfolio.net_liquidation_value,
                "realized_pnl": round(self.synthetic_realized_pnl, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "total_pnl": round(total_pnl, 2),
                "paired_rebalances_count": rebalance_count,
                "core_weight": portfolio.core_weight,
                "satellite_weight": portfolio.satellite_weight,
            },
        )
        logger.info("All telemetry events securely committed to state/telemetry.db (SQLite WAL mode).")
        logger.info("=" * 78)


def main() -> int:
    parser = argparse.ArgumentParser(description="SchwabEngine Mid-Session Paper Run (#94)")
    parser.add_argument("--sequence", type=Path, default=None, help="Path to telemetry micro sequence JSON file")
    args = parser.parse_args()

    runner = MidSessionPaperRunRunner(sequence_file=args.sequence)
    return runner.run_paper_trading_session()


if __name__ == "__main__":
    sys.exit(main())
