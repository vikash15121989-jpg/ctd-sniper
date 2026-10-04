from datetime import datetime, timezone, timedelta
import json
import os
import math
import gspread
import pandas as pd
import numpy as np
import yfinance as yf

# -----------------------------------------------------------------
# TIME SETUP
# -----------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))
now = datetime.now(IST)

print(f"=== [FINAL FLEXIBLE] VPA RBS SCANNER | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

def sanitize_value(val):
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            return 0.0
    return val

def sanitize_rows(rows):
    return [[sanitize_value(val) for val in row] for row in rows]

# -----------------------------------------------------------------
# GOOGLE SHEET CONNECT
# -----------------------------------------------------------------
try:
    gcp_json_creds = json.loads(os.environ["GSHEET_KEY"])
    gc = gspread.service_account_from_dict(gcp_json_creds)
    sh = gc.open("CTD_Sniper")
    print("✅ Connected to Google Sheet: CTD_Sniper", flush=True)
except Exception as e:
    print(f"❌ Error connecting to Google Sheets: {e}")
    exit(1)

def get_or_create_worksheet(title):
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows="500", cols="12")

ETF_KEYWORDS = ["BEES", "ETF", "GOLD", "SILVER", "NIFTY", "NAV", "LIQUID", "IETF", "HANGSENG", "SENSEX", "MON100"]

def is_etf(symbol_name):
    return any(kw in symbol_name.upper() for kw in ETF_KEYWORDS)

# -----------------------------------------------------------------
# WATCHLIST READ
# -----------------------------------------------------------------
try:
    raw_stocks = sh.worksheet("Watchlist").col_values(1)
except Exception as e:
    print(f"❌ Error reading Watchlist: {e}")
    exit(1)

STOCKS = []
for s in raw_stocks:
    if s and s.upper() not in ["STOCK", "SYMBOL", "NAME"]:
        clean_s = s.strip().upper()
        if not clean_s.endswith(".NS"):
            clean_s += ".NS"
        STOCKS.append(clean_s)

clean_stocks = list(set([s for s in STOCKS if not is_etf(s.replace(".NS", ""))]))
print(f"📥 Scanning {len(clean_stocks)} stocks for LL-LH + Vol + Breakout + Pullback...", flush=True)

# -----------------------------------------------------------------
# LINE STRUCTURE FUNCTION (Close Basis)
# -----------------------------------------------------------------
def find_line_structure(df, window=3):
    close_prices = df['Close'].values
    n = len(close_prices)
    swing_highs = []
    swing_lows = []
    for i in range(window, n - window):
        if all(close_prices[i] > close_prices[i - j] for j in range(1, window + 1)) and \
           all(close_prices[i] > close_prices[i + j] for j in range(1, window + 1)):
            swing_highs.append((i, close_prices[i]))
        if all(close_prices[i] < close_prices[i - j] for j in range(1, window + 1)) and \
           all(close_prices[i] < close_prices[i + j] for j in range(1, window + 1)):
            swing_lows.append((i, close_prices[i]))

    is_downtrend = False
    nearest_lh_price = None
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        last_sh = swing_highs[-1][1]
        prev_sh = swing_highs[-2][1]
        last_sl = swing_lows[-1][1]
        prev_sl = swing_lows[-2][1]
        if (last_sh < prev_sh) and (last_sl < prev_sl):
            is_downtrend = True
            nearest_lh_price = last_sh
    return is_downtrend, nearest_lh_price

# -----------------------------------------------------------------
# MAIN SCANNING LOOP
# -----------------------------------------------------------------
filtered_setups = []

for symbol in clean_stocks:
    try:
        stock_clean = symbol.replace(".NS", "")
        df = yf.Ticker(symbol).history(period="70d", interval="1d")
        if df.empty or len(df) < 45:
            continue

        df['Vol_SMA20'] = df['Volume'].rolling(window=20).mean()
        last_close = float(df['Close'].iloc[-1])
        last_high = float(df['High'].iloc[-1])
        last_low = float(df['Low'].iloc[-1])
        last_open = float(df['Open'].iloc[-1])
        last_vol = float(df['Volume'].iloc[-1])
        avg_vol = float(df['Vol_SMA20'].iloc[-1])

        if math.isnan(avg_vol) or avg_vol < 30000 or math.isnan(last_close):
            continue

        vol_ratio = last_vol / avg_vol
        last_spread = last_high - last_low

        # 1. DOWNTREND (LL-LH) - Close Basis
        structure_df = df.iloc[-50:-3]
        is_downtrend, nearest_lh_price = find_line_structure(structure_df, window=3)
        if not is_downtrend or nearest_lh_price is None:
            continue

        # 2+3. VOLUME + BREAKOUT - LOOKBACK WINDOW (Pichhle 7 din)
        lookback_window = df.iloc[-8:-1]

        has_big_volume = False
        has_breakout = False
        has_both_same_day = False

        for i in range(len(lookback_window)):
            row = lookback_window.iloc[i]
            vol_sma = float(row['Vol_SMA20']) if not math.isnan(row['Vol_SMA20']) else 0
            if vol_sma == 0:
                continue

            c_vol_ratio = float(row['Volume']) / vol_sma
            c_close = float(row['Close'])
            c_low = float(row['Low'])
            c_high = float(row['High'])
            c_spread = c_high - c_low

            has_wick = (c_close - c_low) >= (0.35 * c_spread) if c_spread > 0 else False

            # Stopping Volume Check (1.6x + Lower Rejection Wick)
            if c_vol_ratio >= 1.6 and has_wick:
                has_big_volume = True

            # LH Breakout Check (Close Basis)
            if c_close >= nearest_lh_price * 0.998:
                has_breakout = True

            # Dono Ek Hi Din Me (Strongest Trigger)
            if c_vol_ratio >= 1.6 and c_close >= nearest_lh_price * 0.998:
                has_both_same_day = True

        # FIXED LOGIC: Must satisfy either Same-Day OR Both Volume & Breakout in Lookback Window
        if not (has_both_same_day or (has_big_volume and has_breakout)):
            continue

        # 4. PULLBACK / RETEST - Low Volume Pullback on Broken LH
        is_near_support = (last_low <= nearest_lh_price * 1.02) and (last_close >= nearest_lh_price * 0.97)
        is_low_vol = vol_ratio <= 1.15
        has_rejection = (last_close - last_low) >= (0.35 * last_spread) if last_spread > 0 else False
        is_green = last_close > last_open

        is_valid_retest = is_near_support and is_low_vol and (has_rejection or is_green)

        if is_valid_retest:
            if has_both_same_day:
                tag = "STRONG SAME-DAY VOL+BREAKOUT"
            else:
                tag = "FLEXI VOL+BREAKOUT RETEST"

            stop_loss = round(float(df['Low'].iloc[-6:].min() * 0.98), 2)

            filtered_setups.append([
                stock_clean,
                tag,
                round(nearest_lh_price, 2),
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
ws_ready.append_row([
    "Stock", "Tag", "LH_Support", "Last_Close", "StopLoss", "Vol_Status", "Vol_SMA20", "Date"
])

if filtered_setups:
    ws_ready.append_rows(sanitize_rows(filtered_setups))
    print(f"🎯 SUCCESS! Saved {len(filtered_setups)} setups in Google Sheet!")
else:
    ws_ready.append_row(["NO SETUPS TODAY", "-", "-", "-", "-", "-", "-", now.strftime('%d-%b-%Y')])
    print("ℹ️ No setups matched criteria today.")
    
