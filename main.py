import yfinance as yf
import gspread, json, os, math
import pandas as pd
from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")
print(f"=== TRUE SMART MONEY FLOW SCANNER | {datetime.now(IST).strftime('%d-%b %H:%M')} ===")

gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
sh = gc.open("CTD_Sniper")
raw = sh.worksheet("Watchlist").col_values(1)
STOCKS = [s.strip().upper()+".NS" for s in raw if s and len(s.strip())>2 and s.upper() not in ["STOCK","SYMBOL"]][:100]

# Nifty data fetch for Relative Strength check
nifty = yf.Ticker("^NSEI").history(period="1y", auto_adjust=True)
if isinstance(nifty.columns, pd.MultiIndex): nifty.columns = nifty.columns.get_level_values(0)

trades=[]
for sym in STOCKS:
    try:
        d = yf.Ticker(sym).history(period="1y", auto_adjust=True)
        if len(d) < 100: continue
        if isinstance(d.columns, pd.MultiIndex): d.columns = d.columns.get_level_values(0)
        
        # Align with Nifty dates
        common_idx = d.index.intersection(nifty.index)
        if len(common_idx) < 100: continue
        
        stock_close = d.loc[common_idx, 'Close']
        nifty_close = nifty.loc[common_idx, 'Close']
        
        # 1. Accumulation/Distribution (AD) Line Calculation
        clv = ((d['Close'] - d['Low']) - (d['High'] - d['Close'])) / (d['High'] - d['Low'])
        clv = clv.replace([float('inf'), float('-inf')]).fillna(0)
        d['AD_Line'] = (clv * d['Volume']).cumsum()
        
        # 2. Indicators
        d['Vol20'] = d['Volume'].rolling(20).mean()
        d['AD_MA'] = d['AD_Line'].rolling(20).mean()
        d['SMA50'] = d['Close'].rolling(50).mean()
        
        i = 60
        n_rows = len(d)
        while i < n_rows - 15:
            close = d['Close'].iloc[i]
            sma50 = d['SMA50'].iloc[i]
            vol = d['Volume'].iloc[i]
            vol_ma = d['Vol20'].iloc[i]
            
            if pd.isna(sma50) or vol_ma == 0:
                i += 1
                continue
            
            # --- SMART MONEY ABSORPTION LOGIC ---
            # Condition 1: AD Line is above its 20-day MA (Institutional buying pressure active)
            ad_cond = d['AD_Line'].iloc[i] > d['AD_MA'].iloc[i]
            
            # Condition 2: Stealth Absorption (Volume is normal or low, but price is not falling, OR a sudden controlled volume spike near support)
            # Strong close near high of the day (Delivery proxy)
            high_low = d['High'].iloc[i] - d['Low'].iloc[i]
            range_pos = (close - d['Low'].iloc[i]) / high_low if high_low > 0 else 0
            stealth_close = range_pos >= 0.75
            
            # Condition 3: Price is in uptrend (Above SMA50)
            trend_cond = close > sma50
            
            # Condition 4: Relative Strength - Stock outperforming Nifty over last 20 days
            stock_20d = (stock_close.iloc[i] / stock_close.iloc[i-20]) - 1
            nifty_20d = (nifty_close.iloc[i] / nifty_close.iloc[i-20]) - 1
            outperformance = stock_20d > nifty_20d
            
            if ad_cond and stealth_close and trend_cond and outperformance:
                entry = close
                min_low = d['Low'].iloc[i-5:i].min()
                sl = min_low * 0.98
                risk = (entry - sl) / entry * 100
                
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
    print(f"\nSMART MONEY SCANNER RESULTS: Total {len(df)} | 6% Win {win:.1f}% | Avg Days {avg_days:.1f}")
    print(df.tail(20).to_string(index=False))
    
    ws = sh.worksheet("SMART_MONEY_FLOW") if "SMART_MONEY_FLOW" in [x.title for x in sh.worksheets()] else sh.add_worksheet("SMART_MONEY_FLOW", 5000, 10)
    ws.clear()
    ws.append_row([f"Smart Money Flow Scanner | Total {len(df)} | Win {win:.1f}% | Avg {avg_days:.1f}d | {datetime.now(IST).strftime('%d-%b')}"])
    ws.append_row(["Stock", "Date", "Entry", "Risk%", "Hit_6%", "Days"])
    ws.append_rows(df.sort_values("Date", ascending=False).values.tolist())
    print("✅ Results successfully exported to Google Sheet!")
else:
    print("No trades found matching strict institutional accumulation.")
    
