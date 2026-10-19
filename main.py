import yfinance as yf
import gspread, json, os, math
import pandas as pd
from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")
print(f"=== TRANSITION LAUNCHPAD SCANNER | {datetime.now(IST).strftime('%d-%b %H:%M')} ===")

gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
sh = gc.open("CTD_Sniper")
raw = sh.worksheet("Watchlist").col_values(1)
STOCKS = [s.strip().upper()+".NS" for s in raw if s and len(s.strip())>2 and s.upper() not in ["STOCK","SYMBOL"]][:100]

trades=[]
for sym in STOCKS:
    try:
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
            
            # 1. Prior Base / Accumulation Phase: Last 15 days range was tight (max 10% span)
            max_15d = d['High'].iloc[i-15:i].max()
            min_15d = d['Low'].iloc[i-15:i].min()
            tight_base = ((max_15d - min_15d) / min_15d) <= 0.10
            
            # 2. Transition Push (The Launchpad): Volume is 3x+ of average, breaking out of the 15-day range
            vol_push = vol >= (3.0 * vol_ma)
            breakout_trigger = close >= max_15d
            
            # 3. Clean Delivery Close: Close near day's high (Top 15%)
            day_range = high - low
            range_pos = (close - low) / day_range if day_range > 0 else 0
            clean_close = range_pos >= 0.85
            
            # 4. Trend Filter: Above SMA50
            trend_ok = close > sma50
            
            if tight_base and vol_push and breakout_trigger and clean_close and trend_ok:
                entry = close
                sl = min_15d * 0.98  # Stop loss just below the base
                risk = (entry - sl) / entry * 100
                
                if not (1.5 <= risk <= 6.0):
                    i += 1
                    continue
                
                # Forward 15-day simulation for 7% target
                future = d.iloc[i+1:i+16]
                hit = False
                days_taken = 0
                
                for idx, (_, r) in enumerate(future.iterrows(), 1):
                    if r['Low'] <= sl: 
                        break
                    if r['High'] >= entry * 1.07:
                        hit = True
                        days_taken = idx
                        break
                
                trades.append([
                    sym.replace(".NS",""), 
                    d.index[i].strftime('%Y-%m-%d'), 
                    round(entry, 2),
                    round(risk, 2),
                    hit, 
                    days_taken
                ])
                
                i += max(days_taken, 12)
            else:
                i += 1
    except Exception as e:
        continue

df = pd.DataFrame(trades, columns=["Stock", "Date", "Entry", "Risk%", "Hit_7%", "Days"])
if len(df) > 0:
    win = df['Hit_7%'].sum() / len(df) * 100
    avg_days = df[df['Hit_7%']]['Days'].mean() if win > 0 else 0
    print(f"\nLAUNCHPAD TRANSITION SCANNER: Total {len(df)} | 7% Win {win:.1f}% | Avg Days {avg_days:.1f}")
    print(df.tail(20).to_string(index=False))
    
    ws = sh.worksheet("LAUNCHPAD_TRANSITION") if "LAUNCHPAD_TRANSITION" in [x.title for x in sh.worksheets()] else sh.add_worksheet("LAUNCHPAD_TRANSITION", 5000, 10)
    ws.clear()
    ws.append_row([f"Launchpad Transition | Total {len(df)} | Win {win:.1f}% | Avg {avg_days:.1f}d | {datetime.now(IST).strftime('%d-%b')}"])
    ws.append_row(["Stock", "Date", "Entry", "Risk%", "Hit_7%", "Days"])
    ws.append_rows(df.sort_values("Date", ascending=False).values.tolist())
    print("✅ Results exported to Google Sheet!")
else:
    print("No transition launchpad setups found.")
    
