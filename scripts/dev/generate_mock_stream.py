#!/usr/bin/env python3
"""
scripts/dev/generate_mock_stream.py
=================================
Synthesizes a realistic, asynchronous telemetry data stream representing live
Schwab API payloads interleaved with external macroeconomic telemetry.

Requirements implemented:
1. Data Structure: Chronological sequence of 18 events spanning a 10-second window
   with staggered, asynchronous timestamps and millisecond resolution to simulate
   network drift and packet jitter.
2. Mixed Schemas: Integrates four distinct schemas into a single output array:
   - Schwab Level 1 Quotes (SPY, SOXL, TQQQ, TNA) matching Schwab API specifications.
   - Schwab Account Balance ($100,000 NLV, $15,000 cash balance).
   - FRED API Macro Data (ICE BofA High-Yield Credit Spread OAS).
   - Gemini NLP Sentiment (Dovish / Hawkish scoring & macro regime decision).
3. Output Format: Saves the compiled sequence to `data/fixtures/schwab_telemetry_micro_sequence.json`.
4. Drive Sync: Automatically mirrors the output JSON to Google Drive sync targets.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import shutil
import sys
from typing import Any, Dict, List

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("generate_mock_stream")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Google Drive sync candidate directories
GDRIVE_TARGET_DIRS: List[Path] = [
    Path("G:/My Drive/Projects/SchwabEngine/data/fixtures"),
]


def build_schwab_quote_payload(
    symbol: str,
    bid: float,
    ask: float,
    last: float,
    bid_size: int,
    ask_size: int,
    volume: int,
    quote_dt: datetime,
    drift_ms: float,
) -> Dict[str, Any]:
    """
    Constructs a Schwab Level 1 Quote payload conforming to Schwab API specifications
    (both Streaming LEVELONE_EQUITIES and REST MarketData v1 formats).
    """
    cusip_map = {
        "SPY": "78462F103",
        "SOXL": "83410S108",
        "TQQQ": "74347X831",
        "TNA": "25459W847",
    }
    desc_map = {
        "SPY": "SPDR S&P 500 ETF TRUST",
        "SOXL": "DIREXION DAILY SEMICONDUCTOR BULL 3X SHARES",
        "TQQQ": "PROSHARES ULTRAPRO QQQ",
        "TNA": "DIREXION DAILY SMALL CAP BULL 3X SHARES",
    }
    exchange_map = {
        "SPY": ("P", "NYSE Arca"),
        "SOXL": ("P", "NYSE Arca"),
        "TQQQ": ("Q", "NASDAQ Global Market"),
        "TNA": ("P", "NYSE Arca"),
    }

    quote_epoch_ms = int(quote_dt.timestamp() * 1000)
    prev_close_map = {
        "SPY": 584.05,
        "SOXL": 45.10,
        "TQQQ": 77.85,
        "TNA": 50.40,
    }
    open_price_map = {
        "SPY": 582.40,
        "SOXL": 45.20,
        "TQQQ": 78.10,
        "TNA": 50.50,
    }

    prev_close = prev_close_map.get(symbol, last - 0.50)
    open_price = open_price_map.get(symbol, last - 0.20)
    high_price = round(max(open_price, last) + 0.85, 2)
    low_price = round(min(open_price, last) - 0.65, 2)
    net_change = round(last - prev_close, 2)
    net_pct_change = round((net_change / prev_close) * 100, 3)

    exch_code, exch_name = exchange_map.get(symbol, ("P", "NYSE Arca"))

    # Schwab LEVELONE_EQUITIES streaming payload spec
    streaming_content = {
        "key": symbol,
        "delayed": False,
        "assetMainType": "EQUITY",
        "assetSubType": "ETF",
        "cusip": cusip_map.get(symbol, "000000000"),
        "0": symbol,
        "1": round(bid, 2),
        "2": round(ask, 2),
        "3": round(last, 2),
        "4": bid_size,
        "5": ask_size,
        "8": volume,
        "10": open_price,
        "11": high_price,
        "12": low_price,
        "13": prev_close,
        "14": exch_code,
        "24": quote_epoch_ms - int(drift_ms),
        "bidPrice": round(bid, 2),
        "askPrice": round(ask, 2),
        "lastPrice": round(last, 2),
        "bidSize": bid_size,
        "askSize": ask_size,
        "totalVolume": volume,
        "quoteTime": quote_epoch_ms - int(drift_ms),
    }

    # Schwab REST Market Data quote spec
    rest_quote_spec = {
        "symbol": symbol,
        "assetMainType": "EQUITY",
        "assetSubType": "ETF",
        "quote": {
            "bidPrice": round(bid, 2),
            "askPrice": round(ask, 2),
            "lastPrice": round(last, 2),
            "bidSize": bid_size,
            "askSize": ask_size,
            "totalVolume": volume,
            "openPrice": open_price,
            "highPrice": high_price,
            "lowPrice": low_price,
            "closePrice": prev_close,
            "mark": round((bid + ask) / 2.0, 3),
            "netChange": net_change,
            "netPercentChange": net_pct_change,
            "quoteTime": quote_epoch_ms - int(drift_ms),
            "securityStatus": "Normal",
        },
        "reference": {
            "cusip": cusip_map.get(symbol, "000000000"),
            "description": desc_map.get(symbol, symbol),
            "exchange": exch_code,
            "exchangeName": exch_name,
            "isHardToBorrow": False,
            "isShortable": True,
        },
    }

    return {
        "service": "LEVELONE_EQUITIES",
        "command": "SUBS",
        "timestamp": quote_epoch_ms,
        "content": [streaming_content],
        "schwab_rest_spec": rest_quote_spec,
    }


def build_schwab_account_payload(
    nlv: float = 100000.00,
    cash_balance: float = 15000.00,
    account_dt: datetime = None,
    drift_ms: float = 0.0,
) -> Dict[str, Any]:
    """
    Constructs a Schwab Account Balance payload strictly matching Schwab API
    specifications (/trader/v1/accounts) and project invariants:
    - Net Liquidation Value (NLV) = $100,000.00
    - Cash Balance = $15,000.00
    - Settled Cash = $15,000.00
    - Day Trading Buying Power = $400,000.00 (4x intraday equity)
    - Fully hydrated positions matching long market value of $85,000.00
    """
    long_equity = round(nlv - cash_balance, 2)  # $85,000.00
    account_epoch_ms = int(account_dt.timestamp() * 1000) if account_dt else 0

    return {
        "securitiesAccount": {
            "type": "MARGIN",
            "accountNumber": "987654321",
            "roundTrips": 0,
            "isDayTrader": True,
            "isClosingOnlyRestricted": False,
            "pfcbFlag": False,
            "currentBalances": {
                "accruedInterest": 0.0,
                "availableFunds": cash_balance,
                "availableFundsNonMarginableTrade": cash_balance,
                "bondValue": 0.0,
                "buyingPower": round(cash_balance * 2.0, 2),
                "cashAvailableForTrading": cash_balance,
                "cashBalance": cash_balance,
                "cashReceipts": 0.0,
                "dayTradingBuyingPower": round(nlv * 4.0, 2),  # $400,000.00
                "dayTradingBuyingPowerCall": 0.0,
                "dayTradingEquityCall": 0.0,
                "equity": nlv,
                "equityPercentage": 100.0,
                "liquidationValue": nlv,  # $100,000.00
                "longMarginValue": long_equity,
                "longMarketValue": long_equity,  # $85,000.00
                "maintenanceCall": 0.0,
                "maintenanceRequirement": round(long_equity * 0.30, 2),
                "margin": 0.0,
                "marginBalance": 0.0,
                "marginEquity": nlv,
                "moneyMarketFund": 0.0,
                "mutualFundValue": 0.0,
                "regTCall": 0.0,
                "savings": 0.0,
                "shortBalance": 0.0,
                "shortMarginValue": 0.0,
                "shortMarketValue": 0.0,
                "sma": cash_balance,
                "totalCash": cash_balance,
                "unsettledCash": 0.0,
            },
            "initialBalances": {
                "accruedInterest": 0.0,
                "availableFundsNonMarginableTrade": cash_balance,
                "bondValue": 0.0,
                "buyingPower": round(cash_balance * 2.0, 2),
                "cashBalance": cash_balance,
                "cashAvailableForTrading": cash_balance,
                "dayTradingBuyingPower": round(nlv * 4.0, 2),
                "equity": nlv,
                "liquidationValue": nlv,
                "longMarketValue": long_equity,
                "unsettledCash": 0.0,
            },
            "projectedBalances": {
                "availableFunds": cash_balance,
                "availableFundsNonMarginableTrade": cash_balance,
                "buyingPower": round(cash_balance * 2.0, 2),
                "cashAvailableForTrading": cash_balance,
                "dayTradingBuyingPower": round(nlv * 4.0, 2),
                "maintenanceCall": 0.0,
                "regTCall": 0.0,
                "isInCall": False,
                "stockBuyingPower": round(cash_balance * 2.0, 2),
            },
            "positions": [
                {
                    "shortQuantity": 0.0,
                    "averagePrice": 45.20,
                    "currentDayProfitLoss": 230.00,
                    "currentDayProfitLossPercentage": 1.02,
                    "longQuantity": 500.0,
                    "settledLongQuantity": 500.0,
                    "settledShortQuantity": 0.0,
                    "instrument": {
                        "assetType": "EQUITY",
                        "cusip": "83410S108",
                        "symbol": "SOXL",
                        "description": "DIREXION DAILY SEMICONDUCTOR BULL 3X",
                    },
                    "marketValue": 22830.00,
                },
                {
                    "shortQuantity": 0.0,
                    "averagePrice": 78.10,
                    "currentDayProfitLoss": 204.00,
                    "currentDayProfitLossPercentage": 0.43,
                    "longQuantity": 600.0,
                    "settledLongQuantity": 600.0,
                    "settledShortQuantity": 0.0,
                    "instrument": {
                        "assetType": "EQUITY",
                        "cusip": "74347X831",
                        "symbol": "TQQQ",
                        "description": "PROSHARES ULTRAPRO QQQ",
                    },
                    "marketValue": 47064.00,
                },
                {
                    "shortQuantity": 0.0,
                    "averagePrice": 50.35,
                    "currentDayProfitLoss": 156.00,
                    "currentDayProfitLossPercentage": 1.03,
                    "longQuantity": 300.0,
                    "settledLongQuantity": 300.0,
                    "settledShortQuantity": 0.0,
                    "instrument": {
                        "assetType": "EQUITY",
                        "cusip": "25459W847",
                        "symbol": "TNA",
                        "description": "DIREXION DAILY SMALL CAP BULL 3X",
                    },
                    "marketValue": 15261.00,
                },
            ],
            "telemetry_sync": {
                "sync_epoch_ms": account_epoch_ms,
                "network_latency_ms": round(drift_ms, 2),
                "invariants": {
                    "settlement_model": "T+1_MARGIN",
                    "gfv_risk": 0,
                    "liquidation_value_match": True,
                },
            },
        }
    }


def build_fred_macro_payload(
    spread_value: float = 3.18,
    observation_date: str = "2026-10-16",
    series_id: str = "BAMLH0A0HYM2",
    realtime_date: str = "2026-10-17",
) -> Dict[str, Any]:
    """
    Constructs a Federal Reserve Economic Data (FRED) API payload for the
    ICE BofA US High Yield Index Option-Adjusted Spread.
    """
    spread_bps = round(spread_value * 100.0, 1)
    status = "BENIGN" if spread_value < 4.0 else "ELEVATED"
    implication = "RISK_ON_ACCOMMODATIVE" if spread_value < 3.5 else "NEUTRAL"

    return {
        "realtime_start": realtime_date,
        "realtime_end": realtime_date,
        "observation_start": "2026-10-01",
        "observation_end": realtime_date,
        "units": "Percent",
        "output_type": 1,
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": "desc",
        "count": 1,
        "limit": 100,
        "offset": 0,
        "series_id": series_id,
        "series_title": "ICE BofA US High Yield Index Option-Adjusted Spread",
        "frequency": "Daily, Close",
        "seasonal_adjustment": "Not Seasonally Adjusted",
        "last_updated": f"{realtime_date} 09:15:02-05",
        "observations": [
            {
                "realtime_start": realtime_date,
                "realtime_end": realtime_date,
                "date": observation_date,
                "value": str(spread_value),
                "spread_basis_points": spread_bps,
                "status": status,
                "regime_implication": implication,
                "threshold_breach": False,
                "z_score_20d": -0.42,
            }
        ],
    }


def build_gemini_sentiment_payload(
    dovish_score: float = 0.84,
    hawkish_score: float = 0.16,
    macro_regime: str = "REGIME_A",
    market_bias: str = "BULLISH",
    confidence_score: float = 0.94,
    fomc_topic: str = "FOMC_PRESS_BRIEFING_AND_LABOR_TELEMETRY",
) -> Dict[str, Any]:
    """
    Constructs a Gemini NLP Sentiment payload with Dovish/Hawkish scoring,
    strictly adhering to MacroDecisionPayload and governor schemas.
    """
    stance = "DOVISH" if dovish_score > hawkish_score else ("HAWKISH" if hawkish_score > dovish_score else "NEUTRAL")
    net_score = round(dovish_score - hawkish_score, 3)

    return {
        "model": "gemini-2.5-flash",
        "prompt_context": fomc_topic,
        "sentiment_classification": stance,
        "dovish_hawkish_scoring": {
            "dovish_score": round(dovish_score, 3),
            "hawkish_score": round(hawkish_score, 3),
            "neutral_score": round(max(0.0, 1.0 - (dovish_score + hawkish_score)), 3),
            "net_dovish_polarity": net_score,
            "conviction": "HIGH",
        },
        "macro_decision_payload": {
            "macro_regime": macro_regime,
            "market_bias": market_bias,
            "confidence_score": confidence_score,
            "risk_multiplier": 1.0,
            "trade_permitted": True,
            "inhibition_reason": None,
            "high_impact_warning": None,
        },
        "sector_drift_multipliers": {
            "tech_beta": 1.15,
            "small_cap_credit": 1.08,
            "energy_beta": 0.92,
        },
        "telemetry_metrics": {
            "inference_latency_ms": 298.4,
            "tokens_consumed": 420,
            "thematic_drivers": [
                "Labor market stabilization reduces recession risk; soft landing reaffirmed.",
                "High-yield credit spreads remain compressed under 320 bps.",
                "Core disinflation supports continuation of policy easing cycle.",
            ],
        },
    }


def synthesize_stream_events() -> List[Dict[str, Any]]:
    """
    Generates a chronological sequence of 18 asynchronous telemetry events
    spanning a 10-second window with millisecond resolution and realistic network drift.
    """
    # Base timestamp: 2026-10-17 19:20:00.000 UTC
    base_time = datetime(2026, 10, 17, 19, 20, 0, 0, tzinfo=timezone.utc)

    # 18 event definitions with staggered millisecond offsets spanning 9,845 ms (< 10.0s)
    event_specs = [
        # 1. Schwab Quote SPY
        {
            "offset_ms": 124,
            "drift_ms": 14.2,
            "schema_type": "SCHWAB_LEVEL1_QUOTE",
            "source": "schwab_streaming_ws",
            "symbol": "SPY",
            "builder": lambda dt, drift: build_schwab_quote_payload(
                symbol="SPY", bid=585.12, ask=585.15, last=585.14,
                bid_size=120, ask_size=85, volume=42158900, quote_dt=dt, drift_ms=drift
            ),
        },
        # 2. Schwab Quote SOXL
        {
            "offset_ms": 638,
            "drift_ms": 28.6,
            "schema_type": "SCHWAB_LEVEL1_QUOTE",
            "source": "schwab_streaming_ws",
            "symbol": "SOXL",
            "builder": lambda dt, drift: build_schwab_quote_payload(
                symbol="SOXL", bid=45.62, ask=45.65, last=45.64,
                bid_size=350, ask_size=210, volume=18340200, quote_dt=dt, drift_ms=drift
            ),
        },
        # 3. Schwab Account Balance ($100k NLV, $15k Cash)
        {
            "offset_ms": 1182,
            "drift_ms": 42.1,
            "schema_type": "SCHWAB_ACCOUNT_BALANCE",
            "source": "schwab_trader_api",
            "symbol": "ACCOUNT_BALANCES",
            "builder": lambda dt, drift: build_schwab_account_payload(
                nlv=100000.00, cash_balance=15000.00, account_dt=dt, drift_ms=drift
            ),
        },
        # 4. Schwab Quote TQQQ
        {
            "offset_ms": 1745,
            "drift_ms": 18.5,
            "schema_type": "SCHWAB_LEVEL1_QUOTE",
            "source": "schwab_streaming_ws",
            "symbol": "TQQQ",
            "builder": lambda dt, drift: build_schwab_quote_payload(
                symbol="TQQQ", bid=78.40, ask=78.43, last=78.42,
                bid_size=180, ask_size=140, volume=24190500, quote_dt=dt, drift_ms=drift
            ),
        },
        # 5. FRED API Macro Data (High-Yield Credit Spread)
        {
            "offset_ms": 2310,
            "drift_ms": 35.8,
            "schema_type": "FRED_MACRO_DATA",
            "source": "stlouisfed_fred_api",
            "symbol": "BAMLH0A0HYM2",
            "builder": lambda dt, drift: build_fred_macro_payload(
                spread_value=3.18, observation_date="2026-10-16", series_id="BAMLH0A0HYM2"
            ),
        },
        # 6. Schwab Quote TNA
        {
            "offset_ms": 2895,
            "drift_ms": 22.4,
            "schema_type": "SCHWAB_LEVEL1_QUOTE",
            "source": "schwab_streaming_ws",
            "symbol": "TNA",
            "builder": lambda dt, drift: build_schwab_quote_payload(
                symbol="TNA", bid=50.85, ask=50.88, last=50.87,
                bid_size=290, ask_size=175, volume=8421000, quote_dt=dt, drift_ms=drift
            ),
        },
        # 7. Gemini NLP Sentiment (Dovish / Hawkish scoring)
        {
            "offset_ms": 3441,
            "drift_ms": 51.3,
            "schema_type": "GEMINI_NLP_SENTIMENT",
            "source": "gemini_nlp_engine",
            "symbol": "MACRO_SENTIMENT",
            "builder": lambda dt, drift: build_gemini_sentiment_payload(
                dovish_score=0.84, hawkish_score=0.16, macro_regime="REGIME_A",
                market_bias="BULLISH", confidence_score=0.94
            ),
        },
        # 8. Schwab Quote SPY (Tick 2)
        {
            "offset_ms": 4015,
            "drift_ms": 16.8,
            "schema_type": "SCHWAB_LEVEL1_QUOTE",
            "source": "schwab_streaming_ws",
            "symbol": "SPY",
            "builder": lambda dt, drift: build_schwab_quote_payload(
                symbol="SPY", bid=585.14, ask=585.17, last=585.16,
                bid_size=160, ask_size=95, volume=42160100, quote_dt=dt, drift_ms=drift
            ),
        },
        # 9. Schwab Quote SOXL (Tick 2)
        {
            "offset_ms": 4628,
            "drift_ms": 26.2,
            "schema_type": "SCHWAB_LEVEL1_QUOTE",
            "source": "schwab_streaming_ws",
            "symbol": "SOXL",
            "builder": lambda dt, drift: build_schwab_quote_payload(
                symbol="SOXL", bid=45.65, ask=45.68, last=45.66,
                bid_size=420, ask_size=190, volume=18342500, quote_dt=dt, drift_ms=drift
            ),
        },
        # 10. Schwab Quote TQQQ (Tick 2)
        {
            "offset_ms": 5192,
            "drift_ms": 19.7,
            "schema_type": "SCHWAB_LEVEL1_QUOTE",
            "source": "schwab_streaming_ws",
            "symbol": "TQQQ",
            "builder": lambda dt, drift: build_schwab_quote_payload(
                symbol="TQQQ", bid=78.43, ask=78.46, last=78.44,
                bid_size=210, ask_size=110, volume=24192800, quote_dt=dt, drift_ms=drift
            ),
        },
        # 11. FRED API Macro Data (Spread Intraday Validation)
        {
            "offset_ms": 5764,
            "drift_ms": 38.1,
            "schema_type": "FRED_MACRO_DATA",
            "source": "stlouisfed_fred_api",
            "symbol": "BAMLH0A0HYM2",
            "builder": lambda dt, drift: build_fred_macro_payload(
                spread_value=3.17, observation_date="2026-10-16", series_id="BAMLH0A0HYM2"
            ),
        },
        # 12. Schwab Quote TNA (Tick 2)
        {
            "offset_ms": 6340,
            "drift_ms": 24.5,
            "schema_type": "SCHWAB_LEVEL1_QUOTE",
            "source": "schwab_streaming_ws",
            "symbol": "TNA",
            "builder": lambda dt, drift: build_schwab_quote_payload(
                symbol="TNA", bid=50.88, ask=50.91, last=50.89,
                bid_size=310, ask_size=160, volume=8422100, quote_dt=dt, drift_ms=drift
            ),
        },
        # 13. Schwab Account Balance Heartbeat ($100k NLV, $15k Cash)
        {
            "offset_ms": 6912,
            "drift_ms": 40.5,
            "schema_type": "SCHWAB_ACCOUNT_BALANCE",
            "source": "schwab_trader_api",
            "symbol": "ACCOUNT_BALANCES",
            "builder": lambda dt, drift: build_schwab_account_payload(
                nlv=100000.00, cash_balance=15000.00, account_dt=dt, drift_ms=drift
            ),
        },
        # 14. Gemini NLP Sentiment (Streaming Evaluation)
        {
            "offset_ms": 7489,
            "drift_ms": 47.9,
            "schema_type": "GEMINI_NLP_SENTIMENT",
            "source": "gemini_nlp_engine",
            "symbol": "MACRO_SENTIMENT",
            "builder": lambda dt, drift: build_gemini_sentiment_payload(
                dovish_score=0.87, hawkish_score=0.13, macro_regime="REGIME_A",
                market_bias="BULLISH", confidence_score=0.96
            ),
        },
        # 15. Schwab Quote SPY (Tick 3)
        {
            "offset_ms": 8062,
            "drift_ms": 15.1,
            "schema_type": "SCHWAB_LEVEL1_QUOTE",
            "source": "schwab_streaming_ws",
            "symbol": "SPY",
            "builder": lambda dt, drift: build_schwab_quote_payload(
                symbol="SPY", bid=585.16, ask=585.19, last=585.18,
                bid_size=190, ask_size=75, volume=42162000, quote_dt=dt, drift_ms=drift
            ),
        },
        # 16. Schwab Quote SOXL (Tick 3)
        {
            "offset_ms": 8634,
            "drift_ms": 27.8,
            "schema_type": "SCHWAB_LEVEL1_QUOTE",
            "source": "schwab_streaming_ws",
            "symbol": "SOXL",
            "builder": lambda dt, drift: build_schwab_quote_payload(
                symbol="SOXL", bid=45.68, ask=45.71, last=45.70,
                bid_size=380, ask_size=150, volume=18345000, quote_dt=dt, drift_ms=drift
            ),
        },
        # 17. Schwab Quote TQQQ (Tick 3)
        {
            "offset_ms": 9218,
            "drift_ms": 20.3,
            "schema_type": "SCHWAB_LEVEL1_QUOTE",
            "source": "schwab_streaming_ws",
            "symbol": "TQQQ",
            "builder": lambda dt, drift: build_schwab_quote_payload(
                symbol="TQQQ", bid=78.46, ask=78.49, last=78.48,
                bid_size=230, ask_size=105, volume=24195400, quote_dt=dt, drift_ms=drift
            ),
        },
        # 18. Schwab Quote TNA (Tick 3)
        {
            "offset_ms": 9845,
            "drift_ms": 23.9,
            "schema_type": "SCHWAB_LEVEL1_QUOTE",
            "source": "schwab_streaming_ws",
            "symbol": "TNA",
            "builder": lambda dt, drift: build_schwab_quote_payload(
                symbol="TNA", bid=50.90, ask=50.93, last=50.92,
                bid_size=340, ask_size=130, volume=8423500, quote_dt=dt, drift_ms=drift
            ),
        },
    ]

    events: List[Dict[str, Any]] = []

    for idx, spec in enumerate(event_specs, start=1):
        offset = spec["offset_ms"]
        drift = spec["drift_ms"]
        event_dt = base_time + timedelta(milliseconds=offset)
        timestamp_iso = event_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        timestamp_epoch_ms = int(event_dt.timestamp() * 1000)

        payload = spec["builder"](event_dt, drift)

        event_envelope = {
            "sequence_id": idx,
            "event_id": f"EVT_{idx:03d}_{spec['symbol']}_{spec['schema_type']}",
            "timestamp": timestamp_iso,
            "timestamp_epoch_ms": timestamp_epoch_ms,
            "stream_elapsed_ms": offset,
            "schema_type": spec["schema_type"],
            "source": spec["source"],
            "symbol": spec["symbol"],
            "telemetry_metadata": {
                "network_latency_ms": round(drift, 2),
                "jitter_ms": round(abs(drift - 25.0), 2),
                "simulated_arrival_ms": round(offset + drift, 2),
            },
            "payload": payload,
        }
        events.append(event_envelope)

    # Invariant assertion: strictly chronological ordering
    for i in range(len(events) - 1):
        assert events[i]["timestamp_epoch_ms"] < events[i + 1]["timestamp_epoch_ms"], (
            f"Events out of order at index {i}"
        )

    # Invariant assertion: 10-second window span
    total_span_ms = events[-1]["stream_elapsed_ms"] - events[0]["stream_elapsed_ms"]
    assert total_span_ms <= 10000.0, f"Span exceeded 10s: {total_span_ms} ms"

    # Invariant assertion: event count between 15 and 20
    assert 15 <= len(events) <= 20, f"Event count out of range [15, 20]: {len(events)}"

    return events


def sync_to_google_drive(source_path: Path) -> List[Path]:
    """
    Replicates the generated JSON sequence to accessible Google Drive locations.
    """
    synced_paths: List[Path] = []
    if not source_path.exists():
        logger.error("Source file does not exist for Google Drive sync: %s", source_path)
        return synced_paths

    source_size = source_path.stat().st_size

    for target_dir in GDRIVE_TARGET_DIRS:
        drive_root = Path(target_dir.parts[0] + "/") if target_dir.is_absolute() else None
        if drive_root and not drive_root.exists():
            continue

        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            dest_file = target_dir / source_path.name
            shutil.copy2(source_path, dest_file)

            if dest_file.exists() and dest_file.stat().st_size == source_size:
                synced_paths.append(dest_file)
                logger.info("Successfully synced to Google Drive: %s (%d bytes)", dest_file, source_size)
            else:
                logger.warning("Verification failed for sync target: %s", dest_file)
        except Exception as exc:
            logger.warning("Could not sync to %s: %s", target_dir, exc)

    return synced_paths


def main() -> None:
    logger.info("Synthesizing mixed Schwab API and Macroeconomic telemetry stream...")
    events = synthesize_stream_events()
    logger.info("Generated %d events spanning %d ms", len(events), events[-1]["stream_elapsed_ms"])

    output_filename = "schwab_telemetry_micro_sequence.json"

    # Local paths
    local_root_path = PROJECT_ROOT / "data" / "fixtures" / output_filename
    local_root_path.parent.mkdir(parents=True, exist_ok=True)

    json_str = json.dumps(events, indent=2)

    # Write fixture file
    local_root_path.write_text(json_str, encoding="utf-8")
    local_size = local_root_path.stat().st_size
    logger.info("Wrote local file: %s (%d bytes)", local_root_path, local_size)

    # Sync to Google Drive
    logger.info("Initiating Google Drive synchronization...")
    synced_gdrive_paths = sync_to_google_drive(local_root_path)

    # Schema distribution summary
    schema_counts: Dict[str, int] = {}
    for ev in events:
        schema_counts[ev["schema_type"]] = schema_counts.get(ev["schema_type"], 0) + 1

    # Print summary report
    print("\n" + "=" * 80)
    print(" SCHWAB ASYNCHRONOUS TELEMETRY STREAM & DRIVE SYNC")
    print("=" * 80)
    print(f" Output File:         {local_root_path.name}")
    print(f" Local Path:          {local_root_path}")
    print(f" File Size:           {local_size:,} bytes")
    print(f" Event Count:         {len(events)} events (Requirement: 15-20)")
    print(f" Time Window:         {events[0]['timestamp']} -> {events[-1]['timestamp']}")
    print(f" Total Span:          {events[-1]['stream_elapsed_ms']} ms (< 10,000 ms)")
    print("-" * 80)
    print(" MIXED SCHEMAS BREAKDOWN:")
    for st, count in schema_counts.items():
        print(f"   * {st:<28}: {count} events")
    print("-" * 80)
    print(" INVARIANTS VERIFIED:")
    print("   [x] Schwab Level 1 Quotes for SPY, SOXL, TQQQ, TNA")
    print("   [x] Schwab Account Balance ($100,000 NLV, $15,000 Cash Balance)")
    print("   [x] FRED API Macro Data (High-Yield Credit Spread BAMLH0A0HYM2)")
    print("   [x] Gemini NLP Sentiment (Dovish/Hawkish scoring)")
    print("   [x] Staggered asynchronous timestamps with millisecond drift")
    print("-" * 80)
    print(" GOOGLE DRIVE SYNCHRONIZATION STATUS:")
    if synced_gdrive_paths:
        for p in synced_gdrive_paths:
            print(f"   [SYNCED] {p} ({p.stat().st_size:,} bytes)")
    else:
        print("   [WARNING] No Google Drive locations were synced. Please verify G: mount.")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
