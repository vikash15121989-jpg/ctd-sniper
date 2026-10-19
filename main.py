import yfinance as yf
import pandas as pd
from datetime import datetime

print("=== STARTING LOCAL TRANSITION LAUNCHPAD BACKTEST ===")

# Aap apniwatchlist ke kuch stocks yahan daal kar local test kar sakte hain
STOCKS = ["RELIANCE.NS", "TCS.NS", "INFY.NS", "LT.NS", "SBIN.NS", "AXISBANK.NS", "ICICIBANK.NS", "TITAN.NS"]

all_trades = []

for sym in STOCKS:
    try:
        print(f"Backtesting {sym}...")
        d = yf.Ticker(sym).history(period="1y", auto_adjust=True)
        if len(d) < 100: continue
        if isinstance(d.columns, pd.MultiIndex): d.columns = d.columns.get_level_values(0)
        
        d['Vol20'] = d['Volume'].rolling(20).mean()
        d['SMA50'] = d['Close'].rolling(50).mean()
        
        i = 50
        n_rows = len(d)
        while i < n_rows - 15:
            close = d['Close'].iloc[i]
            high = d['High'].iloc[i]
            low = d['Low'].iloc[i]
            vol = d['Volume'].iloc[i]
            vol_ma = d['Vol20'].iloc[i]
            sma50 = d['SMA50'].iloc[i]
            
            if pd.isna(sma50) or vol_ma == 0:
                i += 1
                continue
            
            # 1. Accumulation Base (Last 15 days tight range <= 10%)
            max_15d = d['High'].iloc[i-15:i].max()
            min_15d = d['Low'].iloc[i-15:i].min()
            tight_base = ((max_15d - min_15d) / min_15d) <= 0.10
            
            # 2. Transition Launchpad Push
            vol_push = vol >= (3.0 * vol_ma)
            breakout_trigger = close >= max_15d
            
            # 3. Clean Delivery Close (Top 15% of day range)
            day_range = high - low
            range_pos = (close - low) / day_range if day_range > 0 else 0
            clean_close = range_pos >= 0.85
            
            # 4. Trend Filter
            trend_ok = close > sma50
            
            if tight_base and vol_push and breakout_trigger and clean_close and trend_ok:
                entry = close
                sl = min_15d * 0.98
                risk_pct = (entry - sl) / entry * 100
                
                if not (1.5 <= risk_pct <= 6.0):
                    i += 1
                    continue
                
                # Forward 15-day simulation for 7% Target
                future = d.iloc[i+1:i+16]
                hit_target = False
                hit_sl = False
                exit_price = entry
                days_taken = 0
                
                for idx, (_, r) in enumerate(future.iterrows(), 1):
                    if r['Low'] <= sl:
                        hit_sl = True
                        exit_price = sl
                        days_taken = idx
                        break
                    if r['High'] >= entry * 1.07:
                        hit_target = True
                        exit_price = entry * 1.07
                        days_taken = idx
                        break
                
                if not hit_target and not hit_sl:
                    # Exit at close of 15th day if neither hit
                    exit_price = future.iloc[-1]['Close']
                    days_taken = 15
                
                pnl_pct = (exit_price - entry) / entry * 100
                
                all_trades.append({
                    "Stock": sym.replace(".NS", ""),
                    "Date": d.index[i].strftime('%Y-%m-%d'),
                    "Entry": round(entry, 2),
                    "Risk%": round(risk_pct, 2),
                    "Target_Hit": hit_target,
                    "PnL%": round(pnl_pct, 2),
                    "Days": days_taken
                })
                
                i += max(days_taken, 12)
            else:
                i += 1
    except Exception as e:
        continue

# Backtest Performance Summary
df_res = pd.DataFrame(all_trades)
if len(df_res) > 0:
    total_trades = len(df_res)
    winning_trades = df_res['Target_Hit'].sum()
    win_rate = (winning_trades / total_trades) * 100
    avg_pnl = df_res['PnL%'].mean()
    
    print("\n" + "="*40)
    print("       BACKTEST PERFORMANCE REPORT       ")
    print("="*40)
    print(f"Total Trades Executed : {total_trades}")
    print(f"Winning Trades (7%)   : {winning_trades}")
    print(f"Win Rate              : {win_rate:.2f}%")
    print(f"Average PnL per Trade : {avg_pnl:.2f}%")
    print("="*40)
    print("\nDetailed Trade Log:")
    print(df_res.to_string(index=False))
else:
    print("No trades triggered during backtest.")
    
