from datetime import datetime, timezone, timedelta
import json
import os
import math
import gspread
import pandas as pd
import numpy as np
import yfinance as yf

# IST Timezone setup
IST = timezone(timedelta(hours=5, minutes=30))
now = datetime.now(IST)
print(f"=== [STRATEGY A: BREAKOUT + RETEST SCANNER + HYPERLINK] | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

def sanitize_value(val):
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            return 0.0
    return val

def sanitize_rows(rows):
    return [[sanitize_value(v) for v in row] for row in rows]

# Google Sheets Connection
try:
    gcp_json_creds = json.loads(os.environ["GSHEET_KEY"])
    gc = gspread.service_account_from_dict(gcp_json_creds)
    sh = gc.open("CTD_Sniper")
    print("✅ Google Sheets se Connect ho gaya!", flush=True)
except Exception as e:
    print(f"❌ Connection Error: {e}")
    exit(1)

def get_or_create_worksheet(title):
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows="500", cols="12")

# ETF Exclusion Filter
ETF_KEYWORDS = ["BEES", "ETF", "GOLD", "SILVER", "NIFTY", "NAV", "LIQUID", "IETF", "HANGSENG", "SENSEX", "MON100"]
def is_etf(s):
    return any(kw in s.upper() for kw in ETF_KEYWORDS)

# Load Watchlist
raw_stocks = sh.worksheet("Watchlist").col_values(1)
STOCKS = []
for s in raw_stocks:
    if s and s.upper() not in ["STOCK", "SYMBOL", "NAME"]:
        clean_s = s.strip().upper()
        if not clean_s.endswith(".NS"):
            clean_s += ".NS"
        STOCKS.append(clean_s)

clean_stocks = list(set([s for s in STOCKS if not is_etf(s.replace(".NS",""))]))
print(f"📥 Total {len(clean_stocks)} stocks scan ho rahe hain...", flush=True)

filtered_setups = []

for symbol in clean_stocks:
    try:
        stock_clean = symbol.replace(".NS", "")
        
        ticker_obj = yf.Ticker(symbol)
        df = ticker_obj.history(period="60d", interval="1d")
        if df.empty or len(df) < 45:
            continue

        # Indicators Calculation
        df['Vol_SMA20'] = df['Volume'].rolling(20).mean()
        df['Spread'] = df['High'] - df['Low']
        df['Spread_SMA20'] = df['Spread'].rolling(20).mean()

        # Today's Candle Values
        last_close = float(df['Close'].iloc[-1])
        last_high = float(df['High'].iloc[-1])
        last_low = float(df['Low'].iloc[-1])
        last_open = float(df['Open'].iloc[-1])
        last_vol = float(df['Volume'].iloc[-1])
        avg_vol = float(df['Vol_SMA20'].iloc[-1])
        avg_spread = float(df['Spread_SMA20'].iloc[-1])

        # Yesterday's Candle Values
        prev_close = float(df['Close'].iloc[-2])
        prev_open = float(df['Open'].iloc[-2])

        # Liquidity Filters
        if math.isnan(avg_vol) or avg_vol < 40000 or last_close < 50 or math.isnan(avg_spread) or avg_spread <= 0:
            continue
        if avg_vol * last_close < 4000000:
            continue

        vol_ratio = last_vol / avg_vol
        total_spread = last_high - last_low
        last_spread_ratio = total_spread / avg_spread if avg_spread != 0 else 0

        # STEP 1: RESISTANCE IDENTIFICATION
        total_len = len(df)
        start_idx = max(0, total_len - 45)
        end_idx = max(1, total_len - 10)
        past_resistance = float(df['High'].iloc[start_idx:end_idx].max())

        # STEP 2: BREAKOUT CONFIRMATION
        recent_10_days = df.iloc[-10:-1]
        breakout_happened = False
        breakout_price = 0.0

        for i in range(len(recent_10_days)):
            c_close = float(recent_10_days['Close'].iloc[i])
            c_vol = float(recent_10_days['Volume'].iloc[i])
            c_vol_sma = float(recent_10_days['Vol_SMA20'].iloc[i])

            if c_close > past_resistance and c_vol_sma > 0 and (c_vol / c_vol_sma) >= 1.3:
                breakout_happened = True
                breakout_price = past_resistance
                break

        if not breakout_happened:
            continue

        # STEP 3: RETEST ZONE CHECK
        is_at_support_zone = (last_low <= breakout_price * 1.025) and (last_close >= breakout_price * 0.975)

        # STEP 4: RETEST BULLISH CANDLE PATTERNS
        body_bottom = min(last_open, last_close)
        lower_wick = body_bottom - last_low
        is_hammer = (lower_wick >= 0.35 * total_spread) if total_spread > 0 else False

        is_green = last_close > last_open
        is_engulfing = (last_close > prev_open) and (last_open < prev_close) and is_green

        is_bullish_pattern = is_hammer or is_green or is_engulfing

        is_volume_dry = vol_ratio <= 1.10
        is_tight_spread = last_spread_ratio <= 1.30

        # FINAL SETUP APPROVAL
        if is_at_support_zone and is_volume_dry and is_tight_spread and is_bullish_pattern:
            
            stop_loss = round(float(df['Low'].iloc[-5:].min() * 0.985), 2)
            entry_price = round(last_close, 2)
            
            if entry_price > stop_loss:
                risk = entry_price - stop_loss
                target_1 = round(entry_price + (risk * 2.0), 2)
            else:
                stop_loss = round(entry_price * 0.97, 2)
                target_1 = round(entry_price * 1.06, 2)

            pattern_type = "Hammer" if is_hammer else ("Engulfing" if is_engulfing else "Green Candle")
            
            # TradingView Clickable Link Formula
            tv_url = f"https://in.tradingview.com/chart/?symbol=NSE:{stock_clean}"
            chart_formula = f'=HYPERLINK("{tv_url}", "View Chart")'

            filtered_setups.append([
                stock_clean,
                f"RBS ({pattern_type})",
                round(breakout_price, 2),
                entry_price,
                stop_loss,
                target_1,
                f"{round(vol_ratio, 2)}x Dry",
                now.strftime('%d-%b-%Y'),
                chart_formula
            ])
            print(f"🎯 Pattern Found: {stock_clean} | Type: RBS ({pattern_type})", flush=True)

    except Exception as e:
        continue

# GOOGLE SHEET UPDATE
ws_ready = get_or_create_worksheet("Ready_For_Today")
ws_ready.clear()

# Column Headers (I Header is 'Chart')
ws_ready.append_row(
    ["Stock", "Pattern_Type", "Support_Level", "Entry_Price", "StopLoss", "Target_1", "Vol_Ratio", "Date", "Chart"],
    value_input_option="USER_ENTERED"
)

if filtered_setups:
    ws_ready.append_rows(
        sanitize_rows(filtered_setups),
        value_input_option="USER_ENTERED"
    )
    print(f"\n🎯 SUCCESS! Total {len(filtered_setups)} Setups Saved with Clickable Chart Links!")
else:
    ws_ready.append_row(
        ["NO SETUPS TODAY", "-", "-", "-", "-", "-", "-", now.strftime('%d-%b-%Y'), "-"],
        value_input_option="USER_ENTERED"
    )
    print("\nℹ️ Aaj Strategy A ke hisaab se koi stock fit nahi hua.")
    
