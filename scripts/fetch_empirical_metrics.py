"""
scripts/fetch_empirical_metrics.py
Extracts 15-minute price history for SOXL, TQQQ, TNA from Schwab API,
and computes empirical TR, ATR distributions, regime volumes, RVOL ratio, and bar returns.
"""

import os
import sys
import json
import math
import yaml
from pathlib import Path
from datetime import datetime, time as dtime, timedelta
import pytz
import numpy as np

_EDT = pytz.timezone("America/New_York")

def main():
    # Load config and auth
    with open("config/config.yaml") as f:
        cfg = yaml.safe_load(f)

    from core.auth import SchwabAuthManager, SecurityVault
    from data.rest_client import SchwabRestClient

    passphrase = os.getenv("VAULT_PASSPHRASE")
    client_id = os.getenv("SCHWAB_CLIENT_ID")
    client_secret = os.getenv("SCHWAB_CLIENT_SECRET")

    vault_path = Path("schwab_tokens_vault.json")
    vault = SecurityVault(passphrase=passphrase, iterations=600000, vault_path=vault_path)
    auth = SchwabAuthManager(client_id, client_secret, vault, cfg)
    auth.load_tokens()
    rest = SchwabRestClient.from_config(cfg, auth)

    symbols = ["SOXL", "TQQQ", "TNA"]
    results = {}

    now_dt = datetime.now(_EDT)
    # Target 30 trading days (~45 calendar days back)
    end_ms = int(now_dt.timestamp() * 1000)
    start_dt = now_dt - timedelta(days=48)
    start_ms = int(start_dt.timestamp() * 1000)

    for sym in symbols:
        print(f"Fetching 15m data for {sym}...")
        # Try custom date range first
        url = f"{rest._market_data_base}/pricehistory"
        params = {
            "symbol": sym,
            "frequencyType": "minute",
            "frequency": 15,
            "startDate": start_ms,
            "endDate": end_ms,
            "needExtendedHoursData": "false",
        }
        resp = rest._get(url, params=params)
        data = resp.json()
        candles = data.get("candles", [])
        print(f"  Got {len(candles)} candles via startDate/endDate")

        # If candles is empty or error, fallback to period_type=day, period=10 or period_type=month
        if not candles:
            print("  Falling back to period_type='day', period=10...")
            data = rest.get_price_history(sym, period_type="day", period=10, frequency_type="minute", frequency=15)
            candles = data.get("candles", [])
            print(f"  Fallback returned {len(candles)} candles")

        # Filter strictly for regular trading hours (09:30 to 16:00 EDT)
        rth_candles = []
        for c in candles:
            # c['datetime'] is epoch ms
            dt_utc = datetime.utcfromtimestamp(c["datetime"] / 1000).replace(tzinfo=pytz.utc)
            dt_edt = dt_utc.astimezone(_EDT)
            t = dt_edt.time()
            if dtime(9, 30) <= t < dtime(16, 0):
                c["dt_edt"] = dt_edt
                rth_candles.append(c)

        print(f"  Filtered to {len(rth_candles)} RTH candles across {len(set(c['dt_edt'].date() for c in rth_candles))} trading sessions")

        if not rth_candles:
            continue

        # Compute True Ranges (TR)
        # TR = max(H - L, |H - prev_C|, |L - prev_C|)
        trs = []
        returns_pct = []
        for i in range(len(rth_candles)):
            c = rth_candles[i]
            h, l, o, cl = float(c["high"]), float(c["low"]), float(c["open"]), float(c["close"])
            if i == 0:
                tr = h - l
            else:
                prev_c = float(rth_candles[i-1]["close"])
                tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            trs.append(tr)
            
            # Absolute bar return percentage: |Close - Open| / Open * 100
            ret = (abs(cl - o) / o) * 100.0 if o > 0 else 0.0
            returns_pct.append(ret)

        # 14-period ATR series
        atrs = []
        for i in range(14, len(trs) + 1):
            window = trs[i-14:i]
            atrs.append(sum(window) / 14.0)

        # Volume across session regimes
        vol_opening = []   # 09:30 - 09:45
        vol_midday = []    # 11:30 - 14:00
        vol_power = []     # 15:00 - 15:45
        vol_preclose = []  # 15:45 - 16:00

        for c in rth_candles:
            t = c["dt_edt"].time()
            vol = float(c["volume"])
            if dtime(9, 30) <= t < dtime(9, 45):
                vol_opening.append(vol)
            elif dtime(11, 30) <= t < dtime(14, 0):
                vol_midday.append(vol)
            elif dtime(15, 0) <= t < dtime(15, 45):
                vol_power.append(vol)
            elif dtime(15, 45) <= t < dtime(16, 0):
                vol_preclose.append(vol)

        avg_vol_opening = float(np.mean(vol_opening)) if vol_opening else 0.0
        avg_vol_midday = float(np.mean(vol_midday)) if vol_midday else 0.0
        avg_vol_power = float(np.mean(vol_power)) if vol_power else 0.0
        avg_vol_preclose = float(np.mean(vol_preclose)) if vol_preclose else 0.0

        rvol_ratio = (avg_vol_power / avg_vol_midday) if avg_vol_midday > 0 else 0.0

        # Percentiles
        tr_10 = float(np.percentile(trs, 10))
        tr_50 = float(np.percentile(trs, 50))
        tr_90 = float(np.percentile(trs, 90))

        atr_10 = float(np.percentile(atrs, 10)) if atrs else 0.0
        atr_50 = float(np.percentile(atrs, 50)) if atrs else 0.0
        atr_90 = float(np.percentile(atrs, 90)) if atrs else 0.0

        mean_abs_ret = float(np.mean(returns_pct))
        median_abs_ret = float(np.median(returns_pct))

        results[sym] = {
            "trading_days": len(set(c['dt_edt'].date() for c in rth_candles)),
            "total_bars": len(rth_candles),
            "tr_distribution": {"p10": tr_10, "median": tr_50, "p90": tr_90},
            "atr14_distribution": {"p10": atr_10, "median": atr_50, "p90": atr_90},
            "volume_regimes": {
                "opening_range_avg": avg_vol_opening,
                "midday_chop_avg": avg_vol_midday,
                "power_hour_avg": avg_vol_power,
                "pre_close_avg": avg_vol_preclose,
                "power_hour_rvol_ratio": rvol_ratio,
            },
            "bar_returns": {
                "mean_abs_return_pct": mean_abs_ret,
                "median_abs_return_pct": median_abs_ret,
            }
        }

    print("\n--- FINAL COMPUTED METRICS ---")
    print(json.dumps(results, indent=2))

    with open("empirical_metrics.json", "w") as f:
        json.dump(results, f, indent=2)

if __name__ == "__main__":
    main()
