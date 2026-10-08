import pandas as pd  
import numpy as np  
from typing import Dict, Any, List, Tuple  
from math import floor, sqrt  
import sqlite3
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def compute_yang_zhang_atr(df: pd.DataFrame, hwm: float, atr14: float) -> float:  
    if len(df) < 30: return atr14  
      
    tau = sqrt((1/390.0) / 252.0)  
    recent = df.tail(30)  
    
    log_ho = np.log(recent['high'] / recent['open'])  
    log_lo = np.log(recent['low'] / recent['open'])  
    log_hc = np.log(recent['high'] / recent['close'])  
    log_lc = np.log(recent['low'] / recent['close'])  
      
    rs_var = (log_ho * log_hc + log_lo * log_lc).mean()  
    yz_vol = sqrt(max(rs_var, 1e-8))  
      
    distance = hwm * 2.0 * yz_vol * tau  
    p10_atr, p90_atr = atr14 * 0.50, atr14 * 1.50  
      
    return max(min(distance, p90_atr), p10_atr)  

def run_screener_backtest(historical_bars: Dict[str, pd.DataFrame], candidate_universe: Dict[str, Any], initial_nlv: float = 3753.75) -> Dict[str, float]:  
    capital = initial_nlv  
    equity_curve = [capital]  
    trades = []  
      
    all_dates = list(set().union(*(df.index.date for df in historical_bars.values())))  
    all_dates.sort()  
      
    for current_date in all_dates:  
        metrics = {}
          
        for sym, df in historical_bars.items():  
            day_data = df.loc[df.index.date == current_date]
            if day_data.empty or len(day_data) < 30: continue  
                  
            try:  
                open_price = float(day_data.iloc[0]['open'])  
                
                # Fetch true prior day close instead of using open_price * 0.99
                past_data = df.loc[df.index.date < current_date]
                if past_data.empty:
                    prev_close = open_price * 0.99
                else:
                    prev_close = float(past_data.iloc[-1]['close'])
                    
                gap = abs(open_price - prev_close) / prev_close  
                vol = float(day_data.iloc[:15]['volume'].sum())  
                
                # Attain parity with production DynamicScanner screening logic
                adv_shares = candidate_universe.get(sym, {}).get("adv_20d_shares", 1_000_000)
                adv_dollar = adv_shares * prev_close
                rvol_dollar = (vol * open_price) / max(adv_dollar, 1.0)
                
                # Proxy historical spread via metadata median definition
                median_spread = candidate_universe.get(sym, {}).get("median_spread_cents", 1.5) / 100.0
                spread_pct = median_spread / open_price
                
                metrics[sym] = {"gap": gap, "rvol_d": rvol_dollar, "spread": spread_pct}
            except IndexError: continue  
                  
        if not metrics: continue

        gaps, rvols, spreads = [m["gap"] for m in metrics.values()], [m["rvol_d"] for m in metrics.values()], [m["spread"] for m in metrics.values()]
        min_g, max_g = min(gaps), max(gaps)  
        min_r, max_r = min(rvols), max(rvols)
        min_s, max_s = min(spreads), max(spreads)
        
        candidate_scores: List[Tuple[float, str]] = []  
        for sym, m in metrics.items():
            norm_gap = (m["gap"] - min_g) / max(max_g - min_g, 1e-8)  
            norm_rvol = (m["rvol_d"] - min_r) / max(max_r - min_r, 1e-8)
            norm_spread = (m["spread"] - min_s) / max(max_s - min_s, 1e-8)
            
            score = (0.40 * norm_gap) + (0.45 * norm_rvol) - (0.15 * norm_spread)
            candidate_scores.append((score, sym))
            
        candidate_scores.sort(reverse=True)  
        active_universe = [sym for _, sym in candidate_scores[:3]]  
        
        # We simulate the trading day for the selected universe
        for sym in active_universe:  
            df = historical_bars[sym].loc[historical_bars[sym].index.date == current_date]  
            orb_window = df.between_time('09:30', '09:45')  
            if orb_window.empty: continue  
              
            orb_high, orb_low = float(orb_window['high'].max()), float(orb_window['low'].min())  
            atr_approx = orb_high - orb_low  
              
            trade_active = False  
            entry_price, shares, high_water_mark = 0.0, 0, 0.0  
              
            trading_session = df.between_time('09:46', '15:50')  
            for time_idx, row in trading_session.iterrows():  
                high, low = float(row['high']), float(row['low'])  
                  
                if not trade_active:  
                    if high > orb_high:  
                        trade_active = True  
                        entry_price = max(float(row['open']), orb_high)  
                        high_water_mark = entry_price  
                          
                        risk_delta = max(entry_price - orb_low, 0.01)  
                        q_kelly = floor(50.0 / risk_delta)  
                        q_cap = floor((0.33 * capital) / entry_price)  
                        q_cash = floor((capital - 10.00) / entry_price)  
                        shares = min(q_kelly, q_cap, q_cash)  
                else:  
                    high_water_mark = max(high_water_mark, high)  
                    stop_dist = compute_yang_zhang_atr(trading_session.loc[:time_idx], high_water_mark, atr_approx)  
                    dynamic_stop = max(orb_low, high_water_mark - stop_dist)  
                    target = entry_price + 2.5 * (entry_price - orb_low)  
                      
                    if low < dynamic_stop:  
                        exit_price = min(float(row['open']), dynamic_stop)  
                        pnl = (exit_price - entry_price) * shares  
                        capital += pnl; trades.append(pnl); trade_active = False; break  
                    elif high >= target:  
                        exit_price = max(float(row['open']), target)  
                        pnl = (exit_price - entry_price) * shares  
                        capital += pnl; trades.append(pnl); trade_active = False; break  
                          
            if trade_active:  
                exit_price = float(trading_session.iloc[-1]['close'])  
                pnl = (exit_price - entry_price) * shares  
                capital += pnl; trades.append(pnl)  
                  
        equity_curve.append(capital)  
  
    trades_arr = np.array(trades)  
    win_rate = float((trades_arr > 0).mean()) if len(trades) > 0 else 0.0  
    equity_series = pd.Series(equity_curve)  
      
    returns = equity_series.pct_change().dropna()  
    std_dev = float(returns.std())  
    sharpe = float((returns.mean() / std_dev) * sqrt(252)) if std_dev > 0 else 0.0  
      
    return {  
        "Total_PnL": capital - initial_nlv, "Win_Rate": win_rate,  
        "Sharpe_Ratio": sharpe, "Total_Trades": len(trades)  
    }  

if __name__ == "__main__":
    from calibrate_midday_universe import EXPANDED_UNIVERSE
    import os
    
    db_path = "data/historical_candles.db"
    if os.path.exists(db_path):
        logger.info(f"Loading historical data from {db_path}...")
        with sqlite3.connect(db_path) as conn:
            df = pd.read_sql("SELECT * FROM historical_1m", conn)
            
        if not df.empty:
            df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms').dt.tz_localize('UTC').dt.tz_convert('America/New_York')
            df.set_index('datetime', inplace=True)
            df.sort_index(inplace=True)
            
            historical_bars = {}
            for sym, group in df.groupby('symbol'):
                historical_bars[sym] = group
                
            logger.info("Running backtest...")
            results = run_screener_backtest(historical_bars, EXPANDED_UNIVERSE)
            logger.info(f"Backtest Results: {results}")
        else:
            logger.warning("No data found in historical_1m table.")
    else:
        logger.warning(f"Database {db_path} not found. Running synthetic verification to validate engine math & stop logic...")
        dates = pd.bdate_range(end=datetime.now().strftime('%Y-%m-%d'), periods=5)
        dfs = {}
        for sym in list(EXPANDED_UNIVERSE.keys())[:5]:
            records = []
            base_price = 50.0 if sym != 'TQQQ' else 80.0
            for d in dates:
                times = pd.date_range(d.strftime('%Y-%m-%d 09:30:00'), periods=390, freq='1min', tz='America/New_York')
                p = base_price
                for t in times:
                    step = np.random.normal(0.0001, 0.002)
                    open_p = p
                    close_p = open_p * (1 + step)
                    high_p = max(open_p, close_p) * (1 + abs(np.random.normal(0, 0.001)))
                    low_p = min(open_p, close_p) * (1 - abs(np.random.normal(0, 0.001)))
                    vol = int(np.random.uniform(5000, 50000))
                    p = close_p
                    records.append({'datetime': t, 'open': open_p, 'high': high_p, 'low': low_p, 'close': close_p, 'volume': vol})
            df = pd.DataFrame(records).set_index('datetime')
            dfs[sym] = df
        results = run_screener_backtest(dfs, EXPANDED_UNIVERSE)
        logger.info(f"Verification Backtest Results: {results}")
