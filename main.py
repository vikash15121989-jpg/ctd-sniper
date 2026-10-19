import yfinance as yf
import gspread, json, os, math
import pandas as pd
from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")
print(f"=== TRUE FLOAT TURNOVER & DELIVERY FLOW SCANNER | {datetime.now(IST).strftime('%d-%b %H:%M')} ===")

gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
sh = gc.open("CTD_Sniper")
raw = sh.worksheet("Watchlist").col_values(1)
STOCKS = [s.strip().upper()+".NS" for s in raw if s and len(s.strip())>2 and s.upper() not in ["STOCK","SYMBOL"]][:100]

trades=[]
for sym in STOCKS:
    try:
        t = yf.Ticker(sym)
        d = t.history(period="1y", auto_adjust=True)
        if len(d) < 100: continue
        if isinstance(d.columns, pd.MultiIndex): d.columns = d.columns.get_level_values(0)
        
        # Get total shares outstanding for turnover calculation
        info = t.info
        shares_out = info.get('sharesOutstanding', None)
        if not shares_out or shares_out == 0:
            shares_out = d['Volume'].mean() * 100
            
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
            
            # 1. Free Float Turnover %: Daily volume vs Total Shares
            turnover_pct = (vol / shares_out) * 100
            
            # Heavy institutional delivery condition (High turnover + volume spike)
            heavy_turnover = turnover_pct >= 0.4 or vol >= (2.5 * vol_ma)
            
            # 2. Clean Delivery Close (Top 18% of day range - no selling pressure)
            day_range = high - low
            range_pos = (close - low) / day_range if day_range > 0 else 0
            delivery_close = range_pos >= 0.82
            
            # 3. Trend Filter
            trend_ok = close > sma50
            
            if heavy_turnover and delivery_close and trend_ok:
                entry = close
                min_low = d['Low'].iloc[i-5:i].min()
                sl = min_low * 0.98
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
    print(f"\nFLOAT TURNOVER SCANNER: Total {len(df)} | 7% Win {win:.1f}% | Avg Days {avg_days:.1f}")
    print(df.tail(20).to_string(index=False))
    
    ws = sh.worksheet("FLOAT_TURNOVER") if "FLOAT_TURNOVER" in [x.title for x in sh.worksheets()] else sh.add_worksheet("FLOAT_TURNOVER", 5000, 10)
    ws.clear()
    ws.append_row([f"Float Turnover Flow | Total {len(df)} | Win {win:.1f}% | Avg {avg_days:.1f}d | {datetime.now(IST).strftime('%d-%b')}"])
    ws.append_row(["Stock", "Date", "Entry", "Risk%", "Hit_7%", "Days"])
    ws.append_rows(df.sort_values("Date", ascending=False).values.tolist())
    print("✅ Results exported to Google Sheet!")
else:
    print("No float turnover accumulation setups found.")
    
