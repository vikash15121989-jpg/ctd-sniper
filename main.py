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
print(f"=== [UPTREND PULLBACK SNIPER BACKTEST] | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

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

# AUTO-CREATE SHEET LOGIC
def get_or_create_worksheet(title):
    try:
        ws = sh.worksheet(title)
        print(f"ℹ️ Sheet '{title}' mil gayi hai.", flush=True)
        return ws
    except gspread.exceptions.WorksheetNotFound:
        print(f"⚠️ Sheet '{title}' nahi mili. Automatic create ki ja rahi hai...", flush=True)
        return sh.add_worksheet(title=title, rows="5000", cols="10")

# ETF Exclusion Filter
ETF_KEYWORDS = ["BEES", "ETF", "GOLD", "SILVER", "NIFTY", "NAV", "LIQUID", "IETF", "HANGSENG", "SENSEX", "MON100"]
def is_etf(s):
    return any(kw in s.upper() for kw in ETF_KEYWORDS)

# 1. READ WATCHLIST FROM GOOGLE SHEET
raw_stocks = sh.worksheet("Watchlist").col_values(1)
STOCKS = []
for s in raw_stocks:
    if s and s.upper() not in ["STOCK", "SYMBOL", "NAME"]:
        clean_s = s.strip().upper()
        if not clean_s.endswith(".NS"):
            clean_s += ".NS"
        STOCKS.append(clean_s)

clean_stocks = list(set([s for s in STOCKS if not is_etf(s.replace(".NS",""))]))
print(f"📥 Total {len(clean_stocks)} stocks scan ho rahe hain (Uptrend Pullback Mode)...", flush=True)

backtest_results = []
target_hits = 0
sl_hits = 0

for symbol in clean_stocks:
    try:
        stock_clean = symbol.replace(".NS", "")
        ticker_obj = yf.Ticker(symbol)
        
        # 1-Year Daily Data Fetch
        df = ticker_obj.history(period="1y", interval="1d")
        if df.empty or len(df) < 100:
            continue

        # Indicator Calculations
        df['EMA_20'] = df['Close'].ewm(span=20, adjust=False).mean()
        df['EMA_50'] = df['Close'].ewm(span=50, adjust=False).mean()
        
        # ATR (True Range) Calculation for Dynamic SL
        df['TR'] = np.maximum(
            df['High'] - df['Low'],
            np.maximum(
                abs(df['High'] - df['Close'].shift(1)),
                abs(df['Low'] - df['Close'].shift(1))
            )
        )
        df['ATR_14'] = df['TR'].rolling(window=14).mean()

        for i in range(50, len(df) - 10):
            prev_day = df.iloc[i-1]   # Day 1 (Pullback Red Candle)
            curr_day = df.iloc[i]     # Day 2 (Reversal Bullish Candle)
            
            c_vol = float(curr_day['Volume'])
            p_vol = float(prev_day['Volume'])
            c_close = float(curr_day['Close'])
            c_turnover = c_vol * c_close

            # -------------------------------------------------------------
            # FILTER 1: LIQUIDITY (Daily Vol >= 5Lakh AND Turnover >= ₹3 Cr)
            # -------------------------------------------------------------
            if c_vol < 500000 or c_turnover < 30000000:
                continue

            # -------------------------------------------------------------
            # FILTER 2: TREND ALIGNMENT (20 EMA > 50 EMA)
            # -------------------------------------------------------------
            if curr_day['EMA_20'] <= curr_day['EMA_50']:
                continue  # Skip if stock is in Downtrend

            # -------------------------------------------------------------
            # FILTER 3: VOLUME ABSORPTION (Current Vol >= 1.5x Prev Vol)
            # -------------------------------------------------------------
            if c_vol < (1.5 * p_vol):
                continue

            # Day 1 Attributes (Red Candle in Pullback)
            d1_open, d1_close = float(prev_day['Open']), float(prev_day['Close'])
            d1_body = d1_open - d1_close
            if d1_close >= d1_open or d1_body <= 0:
                continue

            # Day 2 Attributes (Green Reversal Candle)
            d2_open, d2_close = float(curr_day['Open']), float(curr_day['Close'])
            d2_low = float(curr_day['Low'])
            
            if d2_close <= d2_open:
                continue

            pattern_found = None
            close_diff_pct = abs(d2_close - d1_close) / d1_close
            d1_midpoint = d1_close + (d1_body * 0.50)

            # Strict Pattern Logic
            if close_diff_pct <= 0.005:
                pattern_found = "Counterattack Line (Pullback)"
            elif d1_midpoint <= d2_close < d1_open:
                pattern_found = "Piercing Line (Pullback)"

            if pattern_found:
                entry_price = round(d2_close, 2)
                
                # Dynamic ATR Stop Loss (1.5 x ATR below Low)
                atr_val = curr_day['ATR_14'] if not math.isnan(curr_day['ATR_14']) else entry_price * 0.02
                stop_loss = round(max(d2_low - (1.5 * atr_val), entry_price * 0.95), 2)
                
                risk = entry_price - stop_loss
                if risk <= 0:
                    continue

                target_1 = round(entry_price + (risk * 2.0), 2)  # 1:2 Risk-Reward Target

                # 10-Day Forward Simulation
                future_days = df.iloc[i+1:i+11]
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
                print(f"🎯 Sniper Setup: {stock_clean} | {pattern_found} | Result: {outcome}", flush=True)

    except Exception:
        continue

print(f"\n📊 FINAL BACKTEST SUMMARY:")
print(f"Total Trades Found: {len(backtest_results)}")
print(f"Target Hits 🎯: {target_hits}")
print(f"Stop Loss Hits ❌: {sl_hits}")

# GOOGLE SHEET UPDATE
ws_backtest = get_or_create_worksheet("Weekly_Backtest")
ws_backtest.clear()

ws_backtest.append_row(
    ["Stock", "Pattern_Type", "Entry_Price", "StopLoss", "Target_1:2", "Outcome", "Trigger_Date", "Chart"],
    value_input_option="USER_ENTERED"
)

if backtest_results:
    ws_backtest.append_rows(sanitize_rows(backtest_results), value_input_option="USER_ENTERED")
    print(f"\n✅ SUCCESS! Backtest Results Exported to 'Weekly_Backtest' Tab!")
    
