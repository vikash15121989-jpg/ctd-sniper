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
print(f"=== [WEEKLY GAP-DOWN PULLBACK REVERSAL] | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

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
        return sh.add_worksheet(title=title, rows="5000", cols="10")

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
print(f"📥 Total {len(clean_stocks)} stocks scan ho rahe hain (Weekly Gap-Down Reversal)...", flush=True)

backtest_results = []
target_hits = 0
sl_hits = 0

for symbol in clean_stocks:
    try:
        stock_clean = symbol.replace(".NS", "")
        ticker_obj = yf.Ticker(symbol)
        
        # 2-Year Weekly Data Fetch
        df = ticker_obj.history(period="2y", interval="1wk")
        if df.empty or len(df) < 52:
            continue

        for i in range(12, len(df) - 8):
            prev_week = df.iloc[i-1]   # Week 1 (Red Candle)
            curr_week = df.iloc[i]     # Week 2 (Gap Down & Reversal Candle)
            
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
            swing_high = df['High'].iloc[max(0, i-12):i-1].max()
            current_high_ref = prev_week['Close']
            drop_pct = (swing_high - current_high_ref) / swing_high
            
            if drop_pct < 0.15:  # Must have fallen at least 15%
                continue

            # Week 1 Attributes (Must be a Red Candle)
            w1_open, w1_close = float(prev_week['Open']), float(prev_week['Close'])
            w1_body = w1_open - w1_close
            if w1_close >= w1_open or w1_body <= 0:
                continue

            # Week 2 Attributes
            w2_open = float(curr_week['Open'])
            w2_low = float(curr_week['Low'])
            
            # -------------------------------------------------------------
            # FILTER 3: MINIMUM 1% GAP DOWN AFTER RED CANDLE
            # -------------------------------------------------------------
            # Week 2 Open should be at least 1% lower than Week 1 Close
            gap_down_pct = (w1_close - w2_open) / w1_close
            if gap_down_pct < 0.01:
                continue

            # Week 2 must close green (higher than its open)
            if w2_close <= w2_open:
                continue

            pattern_found = None
            close_diff_pct = abs(w2_close - w1_close) / w1_close
            w1_midpoint = w1_close + (w1_body * 0.50)

            # -------------------------------------------------------------
            # FILTER 4: PATTERN CLASSIFICATION (Piercing / Counterattack)
            # -------------------------------------------------------------
            if close_diff_pct <= 0.01:  # Closes near previous close
                pattern_found = "Weekly Gap-Down Counterattack"
            elif w2_close >= w1_midpoint:  # Pierces above midpoint
                pattern_found = "Weekly Gap-Down Piercing Line"

            if pattern_found:
                entry_price = round(w2_close, 2)
                stop_loss = round(w2_low * 0.98, 2)  # 2% buffer below gap-down low
                risk = entry_price - stop_loss
                if risk <= 0:
                    continue

                target_1 = round(entry_price + (risk * 2.5), 2)  # 1:2.5 Risk-Reward

                # 8-Week Forward Simulation
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

                if hit_target:
                    outcome = "Target 1:2.5 Hit 🎯"
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
                print(f"🎯 Gap-Down Setup: {stock_clean} | {pattern_found} | Gap: {gap_down_pct*100:.2f}% | Result: {outcome}", flush=True)

    except Exception:
        continue

print(f"\n📊 GAP-DOWN BACKTEST SUMMARY:")
print(f"Total Setups Found: {len(backtest_results)}")
print(f"Target Hits 🎯: {target_hits}")
print(f"Stop Loss Hits ❌: {sl_hits}")

# GOOGLE SHEET UPDATE
ws_backtest = get_or_create_worksheet("Weekly_Backtest")
ws_backtest.clear()

ws_backtest.append_row(
    ["Stock", "Pattern_Type", "Entry_Price", "StopLoss", "Target_1:2.5", "Outcome", "Trigger_Date", "Chart"],
    value_input_option="USER_ENTERED"
)

if backtest_results:
    ws_backtest.append_rows(sanitize_rows(backtest_results), value_input_option="USER_ENTERED")
    print(f"\n✅ SUCCESS! Results Exported to 'Weekly_Backtest' Tab!")
else:
    ws_backtest.append_row(["NO PATTERNS FOUND", "-", "-", "-", "-", "-", now.strftime('%d-%b-%Y'), "-"], value_input_option="USER_ENTERED")
    print("\nℹ️ Criteria match karne wale koi setups nahi mile.")
    
