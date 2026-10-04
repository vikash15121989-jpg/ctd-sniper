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
print(f"=== [VPA FINAL QUALITY - NO CHOP] | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

def sanitize_value(val):
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            return 0.0
    return val
def sanitize_rows(rows): return [[sanitize_value(v) for v in row] for row in rows]

try:
    gcp_json_creds = json.loads(os.environ["GSHEET_KEY"])
    gc = gspread.service_account_from_dict(gcp_json_creds)
    sh = gc.open("CTD_Sniper")
    print("✅ Connected", flush=True)
except Exception as e:
    print(f"❌ Error: {e}"); exit(1)

def get_or_create_worksheet(title):
    try: return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows="500", cols="12")

ETF_KEYWORDS = ["BEES", "ETF", "GOLD", "SILVER", "NIFTY", "NAV", "LIQUID", "IETF", "HANGSENG", "SENSEX", "MON100"]
def is_etf(s): return any(kw in s.upper() for kw in ETF_KEYWORDS)

raw_stocks = sh.worksheet("Watchlist").col_values(1)
STOCKS = []
for s in raw_stocks:
    if s and s.upper() not in ["STOCK", "SYMBOL", "NAME"]:
        clean_s = s.strip().upper()
        if not clean_s.endswith(".NS"): clean_s += ".NS"
        STOCKS.append(clean_s)
clean_stocks = list(set([s for s in STOCKS if not is_etf(s.replace(".NS",""))]))
print(f"📥 Scanning {len(clean_stocks)} stocks...", flush=True)

# --- FIXED LINE STRUCTURE WITH GAP ---
def find_line_structure(df, window=5):
    close_prices = df['Close'].values
    n = len(close_prices)
    swing_highs = []
    swing_lows = []
    for i in range(window, n - window):
        if all(close_prices[i] > close_prices[i-j] for j in range(1, window+1)) and \
           all(close_prices[i] > close_prices[i+j] for j in range(1, window+1)):
            swing_highs.append((i, close_prices[i]))
        if all(close_prices[i] < close_prices[i-j] for j in range(1, window+1)) and \
           all(close_prices[i] < close_prices[i+j] for j in range(1, window+1)):
            swing_lows.append((i, close_prices[i]))

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return False, None

    # GAP FILTER - POWERMECH FIX
    last_idx, last_sh = swing_highs[-1]
    prev_idx, prev_sh = swing_highs[-2]
    if (last_idx - prev_idx) < 10: # 10 din se pehle wala LH nahi chalega
        return False, None

    last_sl = swing_lows[-1][1]
    prev_sl = swing_lows[-2][1]

    # Significant LH-LL chahiye, 1-2% ka nahi
    if (last_sh < prev_sh * 0.98) and (last_sl < prev_sl * 0.98):
        return True, last_sh
    return False, None

filtered_setups = []
for symbol in clean_stocks:
    try:
        stock_clean = symbol.replace(".NS","")
        df = yf.Ticker(symbol).history(period="100d", interval="1d")
        if df.empty or len(df) < 60: continue

        df['Vol_SMA20'] = df['Volume'].rolling(20).mean()
        df['Spread'] = df['High'] - df['Low']
        df['Spread_SMA20'] = df['Spread'].rolling(20).mean()
        df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
        df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()

        last_close = float(df['Close'].iloc[-1])
        last_high = float(df['High'].iloc[-1])
        last_low = float(df['Low'].iloc[-1])
        last_open = float(df['Open'].iloc[-1])
        last_vol = float(df['Volume'].iloc[-1])
        avg_vol = float(df['Vol_SMA20'].iloc[-1])
        avg_spread = float(df['Spread_SMA20'].iloc[-1])

        if math.isnan(avg_vol) or avg_vol < 40000 or last_close < 80 or math.isnan(avg_spread) or avg_spread<=0: continue
        # Value filter - 50 lakh daily value
        if avg_vol * last_close < 5000000: continue

        vol_ratio = last_vol / avg_vol
        last_spread = last_high - last_low
        last_spread_ratio = last_spread / avg_spread if avg_spread!=0 else 0

        # 0. TREND CHECK - BASE PE
        base_df = df.iloc[-65:-8]
        if len(base_df)<20: continue
        high_50d_base = float(base_df['High'].max())
        ema20_base = float(base_df['EMA20'].iloc[-1])
        ema50_base = float(base_df['EMA50'].iloc[-1])
        ema20_prev_base = float(base_df['EMA20'].iloc[-12])

        # POWERMECH jaise chop ko rokne ke liye EMA20 < EMA50
        is_clear_downtrend = (float(base_df['Close'].iloc[-1]) <= high_50d_base * 0.93) and (ema20_base < ema20_prev_base) and (ema20_base < ema50_base)
        if not is_clear_downtrend: continue

        # 1. STRUCTURE
        structure_df = df.iloc[-60:-8]
        is_downtrend, nearest_lh_price = find_line_structure(structure_df, window=5)
        if not is_downtrend or nearest_lh_price is None: continue

        # 2. STOPPING + BREAKOUT
        lookback_window = df.iloc[-8:-1]
        has_stopping = False
        has_breakout = False
        breakout_vol_rank_ok = False

        # Top 3 volume check
        max_vol_50 = df['Volume'].iloc[-50:-1].max()
        sorted_vol_50 = df['Volume'].iloc[-50:-1].sort_values(ascending=False).values

        for i in range(len(lookback_window)):
            row = lookback_window.iloc[i]
            vol_sma = float(row['Vol_SMA20']) if not math.isnan(row['Vol_SMA20']) else 0
            spread_sma = float(row['Spread_SMA20']) if not math.isnan(row['Spread_SMA20']) else 0
            if vol_sma==0 or spread_sma==0: continue

            c_vol_ratio = float(row['Volume'])/vol_sma
            c_close = float(row['Close']); c_open=float(row['Open']); c_low=float(row['Low']); c_high=float(row['High'])
            c_spread = c_high-c_low
            if c_spread<=0: continue
            c_spread_ratio = c_spread/spread_sma
            has_lower_wick = (c_close - c_low) >= 0.40*c_spread
            is_strong_close = c_close >= c_low + 0.60*c_spread
            is_green = c_close > c_open

            # Stopping - RED allowed - 1.4x
            if c_vol_ratio >= 1.4 and c_spread_ratio >= 1.0 and has_lower_wick:
                has_stopping = True
                # Volume top 3 me hai kya?
                if float(row['Volume']) >= sorted_vol_50[2] * 0.9:
                    breakout_vol_rank_ok = True

            # Breakout - GREEN + LH cross + 1.0x
            if c_close >= nearest_lh_price*0.998 and is_green and is_strong_close and c_vol_ratio >= 1.0:
                has_breakout = True
                if float(row['Volume']) >= sorted_vol_50[2] * 0.9:
                    breakout_vol_rank_ok = True

        if not (has_stopping and has_breakout): continue
        if not breakout_vol_rank_ok: continue # POWERMECH filter - volume top me nahi to skip

        # 3. RETEST
        is_near_support = (last_low <= nearest_lh_price*1.03) and (last_close >= nearest_lh_price*0.96)
        is_low_vol = vol_ratio <= 1.25
        is_compressed = last_spread_ratio <= 1.30
        has_rejection = (last_close - last_low) >= (0.35*last_spread) if last_spread>0 else False

        if is_near_support and is_low_vol and is_compressed and (has_rejection or (last_close>last_open)):
            stop_loss = round(float(df['Low'].iloc[-6:].min()*0.98),2)
            filtered_setups.append([
                stock_clean, "QUALITY RBS", round(nearest_lh_price,2), round(last_close,2),
                stop_loss, f"{round(vol_ratio,1)}x Dry", int(avg_vol), now.strftime('%d-%b-%Y')
            ])
    except Exception:
        continue

ws_ready = get_or_create_worksheet("Ready_For_Today")
ws_ready.clear()
ws_ready.append_row(["Stock","Tag","LH_Support","Last_Close","StopLoss","Vol_Status","Vol_SMA20","Date"])
if filtered_setups:
    ws_ready.append_rows(sanitize_rows(filtered_setups))
    print(f"🎯 SUCCESS! {len(filtered_setups)} Quality setups!")
else:
    ws_ready.append_row(["NO SETUPS TODAY","-","-","-","-","-","-",now.strftime('%d-%b-%Y')])
    print("ℹ️ No setups today.")
