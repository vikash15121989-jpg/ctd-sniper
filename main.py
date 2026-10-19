import yfinance as yf
import gspread, json, os, math
import pandas as pd
from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")
print(f"=== INSTITUTIONAL FLOW SNIPER | {datetime.now(IST).strftime('%d-%b %H:%M')} ===")

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
            
            # 1. Base Dry Volume Check (Pichle 10 dino mein volume shaant tha)
            prior_vol_avg = d['Volume'].iloc[i-10:i].mean()
            vol_compression = prior_vol_avg <= (1.2 * vol_ma)
            
            # 2. Institutional Explosion (Volume 2.5x + Close near High > 88%)
            vol_surge = vol >= (2.5 * vol_ma)
            day_range = high - low
            range_pos = (close - low) / day_range if day_range > 0 else 0
            clean_close = range_pos >= 0.88
            
            # 3. Structural Strength (Above SMA50 & 20-Day High Breakout)
            max_20d_high = d['High'].iloc[i-20:i].max()
            breakout = close >= max_20d_high
            trend = close > sma50
            
            if vol_surge and clean_close and breakout and trend and vol_compression:
                entry = close
                min_low = d['Low'].iloc[i-5:i].min()
                sl = min_low * 0.98
                risk = (entry - sl) / entry * 100
                
                if not (1.5 <= risk <= 6.0):
                    i += 1
                    continue
                
                # 20-day horizon for 8% target
                future = d.iloc[i+1:i+21]
                hit = False
                days_taken = 0
                
                for idx, (_, r) in enumerate(future.iterrows(), 1):
                    if r['Low'] <= sl: 
                        break
                    if r['High'] >= entry * 1.08:
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
                
                i += max(days_taken, 15)
            else:
                i += 1
    except Exception as e:
        continue

df = pd.DataFrame(trades, columns=["Stock", "Date", "Entry", "Risk%", "Hit_8%", "Days"])
if len(df) > 0:
    win = df['Hit_8%'].sum() / len(df) * 100
    avg_days = df[df['Hit_8%']]['Days'].mean() if win > 0 else 0
    print(f"\nINSTITUTIONAL FLOW SCANNER: Total {len(df)} | 8% Win {win:.1f}% | Avg Days {avg_days:.1f}")
    print(df.tail(20).to_string(index=False))
    
    ws = sh.worksheet("SMART_FLOW_VCP") if "SMART_FLOW_VCP" in [x.title for x in sh.worksheets()] else sh.add_worksheet("SMART_FLOW_VCP", 5000, 10)
    ws.clear()
    ws.append_row([f"Institutional VCP Flow | Total {len(df)} | Win {win:.1f}% | Avg {avg_days:.1f}d | {datetime.now(IST).strftime('%d-%b')}"])
    ws.append_row(["Stock", "Date", "Entry", "Risk%", "Hit_8%", "Days"])
    ws.append_rows(df.sort_values("Date", ascending=False).values.tolist())
    print("✅ Results exported to Google Sheet!")
else:
    print("No clean institutional setups found.")
    
