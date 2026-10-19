import yfinance as yf
import gspread, json, os, math
import pandas as pd
from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")
print(f"=== TRUE DELIVERY & SMART MONEY FLOW SCANNER | {datetime.now(IST).strftime('%d-%b %H:%M')} ===")

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
        
        # Indicators
        d['Vol20'] = d['Volume'].rolling(20).mean()
        d['SMA50'] = d['Close'].rolling(50).mean()
        d['SMA20'] = d['Close'].rolling(20).mean()
        
        i = 50
        n_rows = len(d)
        while i < n_rows - 15:
            close = d['Close'].iloc[i]
            high = d['High'].iloc[i]
            low = d['Low'].iloc[i]
            open_p = d['Open'].iloc[i]
            vol = d['Volume'].iloc[i]
            vol_ma = d['Vol20'].iloc[i]
            sma50 = d['SMA50'].iloc[i]
            
            if pd.isna(sma50) or vol_ma == 0:
                i += 1
                continue
            
            # --- DELIVERY ACCUMULATION PROXY LOGIC ---
            # 1. Volume Surge: Volume 20-day average se kam se kam 1.5x ho
            vol_spike = vol >= (1.5 * vol_ma)
            
            # 2. Strong Delivery Close: Close din ke High ke bilkul paas ho (Top 15% range)
            day_range = high - low
            range_pos = (close - low) / day_range if day_range > 0 else 0
            delivery_close = range_pos >= 0.85
            
            # 3. Tight Body / Absorption: Candle body choti ho ya strong bullish ho, lekin price ne rejection na diya ho
            body_size = abs(close - open_p) / open_p * 100
            
            # 4. Trend Filter: Price 50 SMA ke upar ho
            trend_ok = close > sma50
            
            if vol_spike and delivery_close and trend_ok:
                entry = close
                min_low = d['Low'].iloc[i-5:i].min()
                sl = min_low * 0.98
                risk = (entry - sl) / entry * 100
                
                # Risk guardrail: 1.5% se 5.5% tak ka risk
                if not (1.5 <= risk <= 5.5):
                    i += 1
                    continue
                
                # Forward 15-day simulation for 6% target
                future = d.iloc[i+1:i+16]
                hit = False
                days_taken = 0
                
                for idx, (_, r) in enumerate(future.iterrows(), 1):
                    if r['Low'] <= sl: 
                        break
                    if r['High'] >= entry * 1.06:
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

df = pd.DataFrame(trades, columns=["Stock", "Date", "Entry", "Risk%", "Hit_6%", "Days"])
if len(df) > 0:
    win = df['Hit_6%'].sum() / len(df) * 100
    avg_days = df[df['Hit_6%']]['Days'].mean() if win > 0 else 0
    print(f"\nDELIVERY PROXY SCANNER RESULTS: Total {len(df)} | 6% Win {win:.1f}% | Avg Days {avg_days:.1f}")
    print(df.tail(20).to_string(index=False))
    
    ws = sh.worksheet("DELIVERY_FLOW") if "DELIVERY_FLOW" in [x.title for x in sh.worksheets()] else sh.add_worksheet("DELIVERY_FLOW", 5000, 10)
    ws.clear()
    ws.append_row([f"Delivery Proxy Flow Scanner | Total {len(df)} | Win {win:.1f}% | Avg {avg_days:.1f}d | {datetime.now(IST).strftime('%d-%b')}"])
    ws.append_row(["Stock", "Date", "Entry", "Risk%", "Hit_6%", "Days"])
    ws.append_rows(df.sort_values("Date", ascending=False).values.tolist())
    print("✅ Results successfully exported to Google Sheet!")
else:
    print("No trades found matching delivery accumulation criteria.")
    
