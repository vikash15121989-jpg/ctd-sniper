import yfinance as yf
import gspread, json, os, math
import pandas as pd
from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")
print(f"=== FIXED CMF BACKTEST | {datetime.now(IST).strftime('%d-%b %H:%M')} ===")

gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
sh = gc.open("CTD_Sniper")
raw = sh.worksheet("Watchlist").col_values(1)
STOCKS = [s.strip().upper()+".NS" for s in raw if s and len(s.strip())>2 and s.upper() not in ["STOCK","SYMBOL"]][:100]

trades=[]
for sym in STOCKS:
    try:
        d = yf.Ticker(sym).history(period="1y", auto_adjust=True)
        if len(d) < 50: continue
        if isinstance(d.columns, pd.MultiIndex): d.columns = d.columns.get_level_values(0)
        
        # Safe Money Flow Multiplier with High == Low Division-by-Zero Protection
        high_low_diff = d['High'] - d['Low']
        mf_mult = ((d['Close'] - d['Low']) - (d['High'] - d['Close'])) / high_low_diff
        d['MF_Mult'] = mf_mult.replace([float('inf'), float('-inf')]).fillna(0)
        
        d['MF_Vol'] = d['MF_Mult'] * d['Volume']
        d['CMF'] = d['MF_Vol'].rolling(20).sum() / d['Volume'].rolling(20).sum()
        d['Vol20'] = d['Volume'].rolling(20).mean()
        d['SMA50'] = d['Close'].rolling(50).mean()
        
        i = 50
        n_rows = len(d)
        while i < n_rows - 15:
            cmf = d['CMF'].iloc[i]
            vol_ma = d['Vol20'].iloc[i]
            volx = d['Volume'].iloc[i] / vol_ma if vol_ma > 0 else 0
            close = d['Close'].iloc[i]
            sma50 = d['SMA50'].iloc[i]
            
            if pd.isna(cmf) or pd.isna(sma50):
                i += 1
                continue
            
            # REAL FLOW ENTRY - CMF setup
            if cmf > 0.15 and volx > 1.5 and close > sma50:
                entry = close
                # Stop loss based on previous 5 days lowest low
                min_low = d['Low'].iloc[i-5:i].min()
                sl = min_low * 0.97
                risk = (entry - sl) / entry * 100
                
                if not (2 <= risk <= 6):
                    i += 1
                    continue
                
                # Forward 10-day simulation
                future = d.iloc[i+1:i+11]
                hit = False
                days_taken = 0
                
                for idx, (_, r) in enumerate(future.iterrows(), 1):
                    if r['Low'] <= sl: 
                        break
                    if r['High'] >= entry * 1.05:
                        hit = True
                        days_taken = idx
                        break
                
                trades.append([
                    sym.replace(".NS",""), 
                    d.index[i].strftime('%Y-%m-%d'), 
                    round(cmf, 3), 
                    round(volx, 2), 
                    hit, 
                    days_taken
                ])
                
                # Skip forward past the trade window to prevent overlapping duplicate entries on the same stock
                i += max(days_taken, 10)
            else:
                i += 1
    except Exception as e:
        continue

df = pd.DataFrame(trades, columns=["Stock", "Date", "CMF", "VolX", "Hit_5%", "Days"])
if len(df) > 0:
    win = df['Hit_5%'].sum() / len(df) * 100
    avg_days = df[df['Hit_5%']]['Days'].mean() if win > 0 else 0
    print(f"FIXED CMF BACKTEST: Total {len(df)} | 5% Win {win:.1f}% | Avg Days {avg_days:.1f}")
    print(df.tail(20).to_string(index=False))
    
    ws = sh.worksheet("CMF_BACKTEST") if "CMF_BACKTEST" in [x.title for x in sh.worksheets()] else sh.add_worksheet("CMF_BACKTEST", 5000, 10)
    ws.clear()
    ws.append_row([f"CMF>0.15 Fixed Backtest | Total {len(df)} | Win {win:.1f}% | Avg {avg_days:.1f}d | {datetime.now(IST).strftime('%d-%b')}"])
    ws.append_row(["Stock", "Date", "CMF", "VolX", "Hit_5%", "Days"])
    ws.append_rows(df.sort_values("Date", ascending=False).values.tolist())
    print("✅ Results successfully exported to Google Sheet!")
else:
    print("No trades found")
    
