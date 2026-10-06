from datetime import datetime, timezone, timedelta
import json
import os
import math
import gspread
import pandas as pd
import numpy as np
import yfinance as yf

IST = timezone(timedelta(hours=5, minutes=30))
now = datetime.now(IST)
print(f"=== [NEXT-DAY ACTIONABLE VPA SCANNER] | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

def sanitize_value(val):
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            return 0.0
    return val

def sanitize_rows(rows): 
    return [[sanitize_value(v) for v in row] for row in rows]

try:
    gcp_json_creds = json.loads(os.environ["GSHEET_KEY"])
    gc = gspread.service_account_from_dict(gcp_json_creds)
    sh = gc.open("CTD_Sniper")
    print("✅ Connected to CTD_Sniper Sheet", flush=True)
except Exception as e:
    print(f"❌ Connection Error: {e}")
    exit(1)

def get_or_create_worksheet(title):
    try: 
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows="500", cols="12")

ETF_KEYWORDS = ["BEES", "ETF", "GOLD", "SILVER", "NIFTY", "NAV", "LIQUID", "IETF", "HANGSENG", "SENSEX", "MON100"]
def is_etf(s): 
    return any(kw in s.upper() for kw in ETF_KEYWORDS)

raw_stocks = sh.worksheet("Watchlist").col_values(1)
STOCKS = []
for s in raw_stocks:
    if s and s.upper() not in ["STOCK", "SYMBOL", "NAME"]:
        clean_s = s.strip().upper()
        if not clean_s.endswith(".NS"): 
            clean_s += ".NS"
        STOCKS.append(clean_s)

clean_stocks = list(set([s for s in STOCKS if not is_etf(s.replace(".NS",""))]))
print(f"📥 Scanning {len(clean_stocks)} stocks for Next-Day Entries...", flush=True)

filtered_setups = []

for symbol in clean_stocks:
    try:
        stock_clean = symbol.replace(".NS","")
        df = yf.Ticker(symbol).history(period="60d", interval="1d")
        if df.empty or len(df) < 40: 
            continue

        df['Vol_SMA20'] = df['Volume'].rolling(20).mean()
        df['Spread'] = df['High'] - df['Low']
        df['Spread_SMA20'] = df['Spread'].rolling(20).mean()
        df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()

        last_close = float(df['Close'].iloc[-1])
        last_high = float(df['High'].iloc[-1])
        last_low = float(df['Low'].iloc[-1])
        last_open = float(df['Open'].iloc[-1])
        last_vol = float(df['Volume'].iloc[-1])
        avg_vol = float(df['Vol_SMA20'].iloc[-1])
        avg_spread = float(df['Spread_SMA20'].iloc[-1])

        # Basic Liquidity Filters
        if math.isnan(avg_vol) or avg_vol < 40000 or last_close < 80 or math.isnan(avg_spread) or avg_spread <= 0: 
            continue
        if avg_vol * last_close < 5000000: # 50 Lakh daily turnover
            continue

        vol_ratio = last_vol / avg_vol
        last_spread = last_high - last_low
        last_spread_ratio = last_spread / avg_spread if avg_spread != 0 else 0

        # 1. RECENT RESISTANCE LEVEL (Last 20 Days High - Excluding Today)
        recent_resistance = float(df['High'].iloc[-21:-1].max())

        # 2. POWER VOLUME BLAST IN RECENT 5 DAYS
        # Dekhenge ki kya pichhle 5 dino me koi Volume Spike (>1.5x) aaya hai
        recent_5_df = df.iloc[-6:-1]
        has_recent_buying = False
        for i in range(len(recent_5_df)):
            r_vol = float(recent_5_df['Volume'].iloc[i])
            r_vol_sma = float(recent_5_df['Vol_SMA20'].iloc[i])
            r_close = float(recent_5_df['Close'].iloc[i])
            r_open = float(recent_5_df['Open'].iloc[i])
            
            if r_vol_sma > 0 and (r_vol / r_vol_sma) >= 1.5 and r_close >= r_open:
                has_recent_buying = True
                break

        if not has_recent_buying:
            continue

        # 3. TODAY'S SETUP: DRY VOL & NARROW SPREAD NEAR BREAKOUT (READY TO EXPLODE)
        is_near_breakout = (last_close >= recent_resistance * 0.98) # Resistance ke 2% me ho ya just cross kia ho
        is_dry_volume = vol_ratio <= 1.10 # Aaj volume dry hai (Selling exhausted)
        is_tight_range = last_spread_ratio <= 1.20 # Narrow Range Candle
        is_above_ema20 = last_close > float(df['EMA20'].iloc[-1])

        if is_near_breakout and is_dry_volume and is_tight_range and is_above_ema20:
            stop_loss = round(float(df['Low'].iloc[-5:].min() * 0.985), 2)
            filtered_setups.append([
                stock_clean, 
                "READY NEXT DAY", 
                round(recent_resistance, 2), 
                round(last_close, 2),
                stop_loss, 
                f"{round(vol_ratio, 1)}x Dry", 
                int(avg_vol), 
                now.strftime('%d-%b-%Y')
            ])

    except Exception:
        continue

# -----------------------------------------------------------------
# UPDATE GOOGLE SHEET
# -----------------------------------------------------------------
ws_ready = get_or_create_worksheet("Ready_For_Today")
ws_ready.clear()
ws_ready.append_row(["Stock", "Tag", "Resistance", "Last_Close", "StopLoss", "Vol_Status", "Vol_SMA20", "Date"])

if filtered_setups:
    ws_ready.append_rows(sanitize_rows(filtered_setups))
    print(f"🎯 SUCCESS! {len(filtered_setups)} High-Momentum Ready-to-Move Stocks Found!")
else:
    ws_ready.append_row(["NO SETUPS TODAY", "-", "-", "-", "-", "-", "-", now.strftime('%d-%b-%Y')])
    print("ℹ️ No instant breakout setups found today.")
    
