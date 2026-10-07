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
print(f"=== [WEEKLY REVERSAL PATTERNS BACKTEST SCANNER] | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

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
        return sh.add_worksheet(title=title, rows="1000", cols="10")

# ETF Exclusion Filter
ETF_KEYWORDS = ["BEES", "ETF", "GOLD", "SILVER", "NIFTY", "NAV", "LIQUID", "IETF", "HANGSENG", "SENSEX", "MON100"]
def is_etf(s):
    return any(kw in s.upper() for kw in ETF_KEYWORDS)

# Load Watchlist from Google Sheet
raw_stocks = sh.worksheet("Watchlist").col_values(1)
STOCKS = []
for s in raw_stocks:
    if s and s.upper() not in ["STOCK", "SYMBOL", "NAME"]:
        clean_s = s.strip().upper()
        if not clean_s.endswith(".NS"):
            clean_s += ".NS"
        STOCKS.append(clean_s)

clean_stocks = list(set([s for s in STOCKS if not is_etf(s.replace(".NS",""))]))
print(f"📥 Total {len(clean_stocks)} stocks par 2-Year Weekly Backtest chalu ho raha hai...\n", flush=True)

backtest_results = []

for symbol in clean_stocks:
    try:
        stock_clean = symbol.replace(".NS", "")
        ticker_obj = yf.Ticker(symbol)
        
        # 2 Years of Weekly Data
        df = ticker_obj.history(period="2y", interval="1wk")
        if df.empty or len(df) < 52:
            continue

        for i in range(15, len(df) - 8):
            prev_week = df.iloc[i-1]   # Week 1 (Bearish Candle)
            curr_week = df.iloc[i]     # Week 2 (Bullish Reversal Candle)
            
            # 1. Prior Downtrend Check (Min 8% Correction from 10-week High)
            swing_high = df['High'].iloc[max(0, i-10):i-1].max()
            if prev_week['Close'] >= swing_high * 0.92:
                continue

            # 2. Week 1 Attributes (Red Weekly Candle)
            w1_open, w1_close = float(prev_week['Open']), float(prev_week['Close'])
            w1_body = w1_open - w1_close
            if w1_close >= w1_open or w1_body <= 0:
                continue

            # 3. Week 2 Attributes (Green Weekly Candle + Gap Down / Lower Open)
            w2_open, w2_close = float(curr_week['Open']), float(curr_week['Close'])
            w2_low = float(curr_week['Low'])
            
            if w2_close <= w2_open or w2_open > w1_close * 1.005:
                continue

            pattern_found = None
            close_diff_pct = abs(w2_close - w1_close) / w1_close
            w1_midpoint = w1_close + (w1_body * 0.50)

            # Pattern Conditions
            if close_diff_pct <= 0.007:
                pattern_found = "Weekly Counterattack Line"
            elif w1_midpoint <= w2_close < w1_open:
                pattern_found = "Weekly Piercing Line"

            if pattern_found:
                entry_price = round(w2_close, 2)
                stop_loss = round(w2_low * 0.985, 2)
                risk = entry_price - stop_loss
                if risk <= 0:
                    continue

                target_1 = round(entry_price + (risk * 2.0), 2)  # 1:2 R:R

                # Forward Simulation over next 8 weeks
                future_weeks = df.iloc[i+1:i+9]
                hit_target = False
                hit_sl = False

                for _, f_week in future_weeks.iterrows():
                    if f_week['Low'] <= stop_loss:
                        hit_sl = True
                        break
                    if f_week['High'] >= target_1:
                        hit_target = True
                        break

                outcome = "Target 1:2 Hit 🎯" if hit_target else ("Stop Loss Hit ❌" if hit_sl else "No Result")

                tv_url = f"https://in.tradingview.com/chart/?symbol=NSE:{stock_clean}"
                chart_formula = f'=HYPERLINK("{tv_url}", "View Chart")'

                date_str = df.index[i].strftime('%d-%b-%Y')

                backtest_results.append([
                    stock_clean,
                    pattern_found,
                    entry_price,
                    stop_loss,
                    target_1,
                    outcome,
                    date_str,
                    chart_formula
                ])
                print(f"📌 Found Trade: {stock_clean} | {pattern_found} | Date: {date_str} | Result: {outcome}", flush=True)

    except Exception as e:
        continue

# -------------------------------------------------------------
# GOOGLE SHEET UPDATE
# -------------------------------------------------------------
ws_backtest = get_or_create_worksheet("Weekly_Backtest")
ws_backtest.clear()

# Column Headers
ws_backtest.append_row(
    ["Stock", "Pattern_Type", "Entry_Price", "StopLoss", "Target_1:2", "Outcome", "Trigger_Date", "Chart"],
    value_input_option="USER_ENTERED"
)

if backtest_results:
    ws_backtest.append_rows(
        sanitize_rows(backtest_results),
        value_input_option="USER_ENTERED"
    )
    print(f"\n🎯 SUCCESS! Total {len(backtest_results)} Backtest Trades Exported to Google Sheet ('Weekly_Backtest')!")
else:
    ws_backtest.append_row(
        ["NO PATTERNS FOUND IN BACKTEST", "-", "-", "-", "-", "-", now.strftime('%d-%b-%Y'), "-"],
        value_input_option="USER_ENTERED"
    )
    print("\nℹ️ Backtest period me koi pattern match nahi hua.")
    
