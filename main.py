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
print(f"=== [LIVE WEEKLY GAP-DOWN REVERSAL SCANNER] | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

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
    print("✅ Connected to Google Sheets!", flush=True)
except Exception as e:
    print(f"❌ Connection Error: {e}")
    exit(1)

def get_or_create_worksheet(title):
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows="5000", cols="10")

ETF_KEYWORDS = ["BEES", "ETF", "GOLD", "SILVER", "NIFTY", "NAV", "LIQUID", "IETF", "HANGSENG", "SENSEX", "MON100"]
def is_etf(s):
    return any(kw in s.upper() for kw in ETF_KEYWORDS)

# 1. Read Watchlist
raw_stocks = sh.worksheet("Watchlist").col_values(1)
STOCKS = []
for s in raw_stocks:
    if s and s.upper() not in ["STOCK", "SYMBOL", "NAME"]:
        clean_s = s.strip().upper()
        if not clean_s.endswith(".NS"):
            clean_s += ".NS"
        STOCKS.append(clean_s)

clean_stocks = list(set([s for s in STOCKS if not is_etf(s.replace(".NS",""))]))
print(f"📥 Scanning {len(clean_stocks)} stocks for live weekly setups...", flush=True)

live_signals = []

for symbol in clean_stocks:
    try:
        stock_clean = symbol.replace(".NS", "")
        df = yf.Ticker(symbol).history(period="1y", interval="1wk")
        if df.empty or len(df) < 15:
            continue

        # We look at the latest completed weekly transition (last 2 rows: -2 and -1)
        prev_week = df.iloc[-2]   # Week 1 (Red Candle)
        curr_week = df.iloc[-1]   # Week 2 (Gap-Down Reversal Candle)
        
        w2_vol = float(curr_week['Volume'])
        w2_close = float(curr_week['Close'])
        w2_turnover = w2_vol * w2_close

        # -------------------------------------------------------------
        # FILTER 1: LIQUIDITY (Min 50 Lakh Vol & ₹15 Cr Turnover)
        # -------------------------------------------------------------
        if w2_vol < 5000000 or w2_turnover < 150000000:
            continue

        # -------------------------------------------------------------
        # FILTER 2: 15% MINIMUM CORRECTION FROM RECENT SWING HIGH
        # -------------------------------------------------------------
        swing_high = df['High'].iloc[:-2].max() if len(df) > 12 else df['High'].max()
        current_high_ref = prev_week['Close']
        drop_pct = (swing_high - current_high_ref) / swing_high
        if drop_pct < 0.15:
            continue

        # Week 1 Must be a Strong Red Candle
        w1_open, w1_close = float(prev_week['Open']), float(prev_week['Close'])
        w1_body = w1_open - w1_close
        if w1_close >= w1_open or w1_body <= 0:
            continue

        # Week 2 Attributes
        w2_open = float(curr_week['Open'])
        w2_low = float(curr_week['Low'])
        
        # -------------------------------------------------------------
        # FILTER 3: MINIMUM 1.0% GAP DOWN OPEN AFTER RED CANDLE CLOSE
        # -------------------------------------------------------------
        gap_down_pct = (w1_close - w2_open) / w1_close
        if gap_down_pct < 0.01:
            continue

        # Week 2 must be Green (Close > Open)
        if w2_close <= w2_open:
            continue

        pattern_found = None
        w1_midpoint = w1_close + (w1_body * 0.50)
        close_diff_pct = abs(w2_close - w1_close) / w1_close

        # -------------------------------------------------------------
        # FILTER 4: STRICT GEOMETRY MATCHING
        # -------------------------------------------------------------
        if close_diff_pct <= 0.003:
            pattern_found = "Weekly Strict Counterattack"
        elif w1_midpoint <= w2_close < w1_open:
            pattern_found = "Weekly Strict Piercing Line"

        if pattern_found:
            entry_price = round(w2_close, 2)
            stop_loss = round(w2_low * 0.98, 2)  # 2% buffer below weekly low
            risk = entry_price - stop_loss
            if risk <= 0:
                continue

            target_1 = round(entry_price + (risk * 2.5), 2)  # 1:2.5 Target

            tv_url = f"https://in.tradingview.com/chart/?symbol=NSE:{stock_clean}"
            chart_formula = f'=HYPERLINK("{tv_url}", "View Chart")'
            trigger_date = df.index[-1].strftime('%d-%b-%Y')

            live_signals.append([
                stock_clean, pattern_found, entry_price, stop_loss,
                target_1, f"{gap_down_pct*100:.2f}%", trigger_date, chart_formula
            ])
            print(f"🚨 LIVE SETUP FOUND: {stock_clean} | {pattern_found} | Entry: {entry_price}", flush=True)

    except Exception:
        continue

print(f"\n📊 TOTAL LIVE SIGNALS FOUND TODAY: {len(live_signals)}")

# GOOGLE SHEET UPDATE (`Ready_For_Today`)
ws_ready = get_or_create_worksheet("Ready_For_Today")
ws_ready.clear()

ws_ready.append_row(
    ["Stock", "Pattern_Type", "Entry_Price", "StopLoss", "Target_1:2.5", "Gap_Down_%", "Trigger_Date", "Chart"],
    value_input_option="USER_ENTERED"
)

if live_signals:
    ws_ready.append_rows(sanitize_rows(live_signals), value_input_option="USER_ENTERED")
    print("\n✅ Live Signals Exported to 'Ready_For_Today' Tab Successfully!")
else:
    ws_ready.append_row(["NO LIVE SETUPS FOUND TODAY", "-", "-", "-", "-", "-", now.strftime('%d-%b-%Y'), "-"], value_input_option="USER_ENTERED")
    print("\nℹ️ Aaj koi naya live setup match nahi hua.")
    
