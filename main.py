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
print(f"=== [DAILY HYBRID REVERSAL SCANNER] | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

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

raw_stocks = sh.worksheet("Watchlist").col_values(1)
STOCKS = [s.strip().upper() + (".NS" if not s.strip().upper().endswith(".NS") else "") 
          for s in raw_stocks if s and s.upper() not in ["STOCK", "SYMBOL", "NAME"]]

clean_stocks = list(set([s for s in STOCKS if not is_etf(s.replace(".NS",""))]))

backtest_results = []
target_hits = 0
sl_hits = 0

for symbol in clean_stocks:
    try:
        stock_clean = symbol.replace(".NS", "")
        df = yf.Ticker(symbol).history(period="1y", interval="1d")
        if df.empty or len(df) < 100:
            continue

        for i in range(20, len(df) - 15):
            prev_day = df.iloc[i-1]
            curr_day = df.iloc[i]
            
            c_vol = float(curr_day['Volume'])
            c_close = float(curr_day['Close'])
            c_turnover = c_vol * c_close

            # Liquidity Filter: Vol >= 5Lakh AND Turnover >= ₹3 Cr
            if c_vol < 500000 or c_turnover < 30000000:
                continue

            # Downtrend Filter: 15% Fall from recent 20-day high
            swing_high = df['High'].iloc[max(0, i-20):i-1].max()
            if prev_day['Close'] >= swing_high * 0.85:
                continue

            d1_open, d1_close = float(prev_day['Open']), float(prev_day['Close'])
            d1_body = d1_open - d1_close
            if d1_close >= d1_open or d1_body <= 0:
                continue

            d2_open, d2_close = float(curr_day['Open']), float(curr_day['Close'])
            d2_low = float(curr_day['Low'])
            
            if d2_close <= d2_open or d2_open > d1_close * 1.005:
                continue

            pattern_found = None
            close_diff_pct = abs(d2_close - d1_close) / d1_close
            d1_midpoint = d1_close + (d1_body * 0.50)

            if close_diff_pct <= 0.005:
                pattern_found = "Daily Counterattack Line"
            elif d1_midpoint <= d2_close < d1_open:
                pattern_found = "Daily Piercing Line"

            if pattern_found:
                entry_price = round(d2_close, 2)
                stop_loss = round(d2_low * 0.985, 2)
                risk = entry_price - stop_loss
                if risk <= 0:
                    continue

                target_1 = round(entry_price + (risk * 2.0), 2)

                future_days = df.iloc[i+1:i+16]
                hit_target = False
                hit_sl = False

                for _, f_day in future_days.iterrows():
                    if f_day['Low'] <= stop_loss:
                        hit_sl = True
                        break
                    if f_day['High'] >= target_1:
                        hit_target = True
                        break

                if hit_target:
                    outcome = "Target 1:2 Hit 🎯"
                    target_hits += 1
                elif hit_sl:
                    outcome = "Stop Loss Hit ❌"
                    sl_hits += 1
                else:
                    outcome = "No Result ⏳"

                tv_url = f"https://in.tradingview.com/chart/?symbol=NSE:{stock_clean}"
                chart_formula = f'=HYPERLINK("{tv_url}", "View Chart")'
                date_str = df.index[i].strftime('%d-%b-%Y')

                backtest_results.append([
                    stock_clean, pattern_found, entry_price, stop_loss,
                    target_1, outcome, date_str, chart_formula
                ])
    except Exception:
        continue

print(f"\n📊 FINAL SUMMARY:")
print(f"Total Trades: {len(backtest_results)}")
print(f"Target Hits 🎯: {target_hits}")
print(f"Stop Loss Hits ❌: {sl_hits}")

ws_backtest = get_or_create_worksheet("Weekly_Backtest")
ws_backtest.clear()
ws_backtest.append_row(["Stock", "Pattern_Type", "Entry_Price", "StopLoss", "Target_1:2", "Outcome", "Trigger_Date", "Chart"], value_input_option="USER_ENTERED")
if backtest_results:
    ws_backtest.append_rows(sanitize_rows(backtest_results), value_input_option="USER_ENTERED")
    
