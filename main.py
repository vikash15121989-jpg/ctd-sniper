from datetime import datetime, timezone, timedelta
import json
import os
import math
import gspread
import pandas as pd
import numpy as np
import yfinance as yf

# -----------------------------------------------------------------
# TIME SETUP (IST)
# -----------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))
now = datetime.now(IST)

print(f"=== [VPA PRO] LINE-STRUCTURE + EFFORT-VS-RESULT SCANNER | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

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
print(f"📥 Scanning {len(clean_stocks)} stocks for VPA RBS Setups...", flush=True)

# -----------------------------------------------------------------
# LINE STRUCTURE FUNCTION (Close Basis - Window=3)
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
        
        # Check Lower High & Lower Low Structure
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

        # Indicators: Vol SMA20 & Spread SMA20
        df['Vol_SMA20'] = df['Volume'].rolling(window=20).mean()
        df['Spread'] = df['High'] - df['Low']
        df['Spread_SMA20'] = df['Spread'].rolling(window=20).mean()

        last_close = float(df['Close'].iloc[-1])
        last_high = float(df['High'].iloc[-1])
        last_low = float(df['Low'].iloc[-1])
        last_open = float(df['Open'].iloc[-1])
        last_vol = float(df['Volume'].iloc[-1])
        avg_vol = float(df['Vol_SMA20'].iloc[-1])
        avg_spread = float(df['Spread_SMA20'].iloc[-1])

        if math.isnan(avg_vol) or avg_vol < 30000 or math.isnan(last_close) or math.isnan(avg_spread) or avg_spread <= 0:
            continue

        vol_ratio = last_vol / avg_vol
        last_spread = last_high - last_low
        last_spread_ratio = last_spread / avg_spread

        # 1. DOWNTREND (LL-LH) - Close Basis Line Structure
        structure_df = df.iloc[-50:-3]
        is_downtrend, nearest_lh_price = find_line_structure(structure_df, window=3)
        if not is_downtrend or nearest_lh_price is None:
            continue

        # 2+3. VOLUME BLAST + SPREAD EXPANSION + LH BREAKOUT (Pichhle 7 din me STRICT BINDING)
        lookback_window = df.iloc[-8:-1]
        has_valid_vol_breakout = False

        for i in range(len(lookback_window)):
            row = lookback_window.iloc[i]
            vol_sma = float(row['Vol_SMA20']) if not math.isnan(row['Vol_SMA20']) else 0
            spread_sma = float(row['Spread_SMA20']) if not math.isnan(row['Spread_SMA20']) else 0
            
            if vol_sma == 0 or spread_sma == 0:
                continue

            c_vol_ratio = float(row['Volume']) / vol_sma
            c_close = float(row['Close'])
            c_low = float(row['Low'])
            c_high = float(row['High'])
            c_spread = c_high - c_low
            c_spread_ratio = c_spread / spread_sma

            has_wick = (c_close - c_low) >= (0.35 * c_spread) if c_spread > 0 else False

            # VPA EFFORT VS RESULT CONDITIONS:
            is_breakout = c_close >= (nearest_lh_price * 0.998)
            is_volume_blast = c_vol_ratio >= 1.6                  # Effort (Institutional Buying)
            is_wide_spread = c_spread_ratio >= 1.2                # Result (Price Range Expansion)

            # Breakout wale din hi Volume + Wide Spread + Lower Wick Rejection hona chahiye
            if is_breakout and is_volume_blast and is_wide_spread and has_wick:
                has_valid_vol_breakout = True
                break

        if not has_valid_vol_breakout:
            continue

        # 4. PULLBACK / RETEST - DRY VOLUME + SPREAD COMPRESSION (Aaj/Kal)
        is_near_support = (last_low <= nearest_lh_price * 1.02) and (last_close >= nearest_lh_price * 0.97)
        is_low_vol = vol_ratio <= 1.15                           # Volume Shrank (Dry)
        is_compressed_spread = last_spread_ratio <= 1.25          # Spread Shrank (No Aggressive Selling)
        
        has_rejection = (last_close - last_low) >= (0.35 * last_spread) if last_spread > 0 else False
        is_green = last_close > last_open

        is_valid_retest = is_near_support and is_low_vol and is_compressed_spread and (has_rejection or is_green)

        # SAVE MATCHED SETUPS
        if is_valid_retest:
            tag = "VPA RBS RETEST (VOL+SPREAD CONFIRMED)"
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
    print(f"🎯 SUCCESS! Saved {len(filtered_setups)} high-quality VPA setups in Google Sheet!")
else:
    ws_ready.append_row(["NO SETUPS TODAY", "-", "-", "-", "-", "-", "-", now.strftime('%d-%b-%Y')])
    print("ℹ️ No stocks matched criteria today.")
