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
print(f"=== [CTD_SNIPER: GOOGLE SHEET HISTORICAL BACKTEST] | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

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
        return sh.add_worksheet(title=title, rows="1000", cols="15")

ETF_KEYWORDS = ["BEES", "ETF", "GOLD", "SILVER", "NIFTY", "NAV", "LIQUID", "IETF", "HANGSENG", "SENSEX", "MON100"]
def is_etf(s):
    return any(kw in s.upper() for kw in ETF_KEYWORDS)

# 1. Read Watchlist from Google Sheet
try:
    raw_stocks = sh.worksheet("Watchlist").col_values(1)
except Exception:
    print("⚠️ 'Watchlist' tab not found in Google Sheet!")
    exit(1)

STOCKS = []
for s in raw_stocks:
    if s and s.upper() not in ["STOCK", "SYMBOL", "NAME"]:
        clean_s = s.strip().upper()
        if not clean_s.endswith(".NS"):
            clean_s += ".NS"
        STOCKS.append(clean_s)

clean_stocks = list(set([s for s in STOCKS if not is_etf(s.replace(".NS",""))]))
print(f"📥 Loaded {len(clean_stocks)} stocks from Google Sheet Watchlist for Backtesting...", flush=True)

# Fetch Benchmark Nifty Data for Mansfield RS Calculation
try:
    nifty_df = yf.download("^NSEI", start="2024-01-01", end="2026-10-01", progress=False)
    if isinstance(nifty_df.columns, pd.MultiIndex):
        nifty_df = nifty_df.xs("^NSEI", level=1, axis=1)
    nifty_df['20_EMA'] = nifty_df['Close'].ewm(span=20, adjust=False).mean()
except Exception as e:
    print(f"❌ Nifty Benchmark Fetch Error: {e}")
    exit(1)

all_backtest_trades = []

# Run Backtest on Sheet Watchlist Stocks
for symbol in clean_stocks:
    try:
        stock_clean = symbol.replace(".NS", "")
        print(f"Backtesting: {stock_clean}...", flush=True)
        
        # CRITICAL: auto_adjust=False for unadjusted raw prices matching charts
        df = yf.download(symbol, start="2024-01-01", end="2026-10-01", auto_adjust=False, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 150:
            continue

        # Daily Technical Indicators
        df['20_EMA'] = df['Close'].ewm(span=20, adjust=False).mean()

        # Resample to Monthly Candles using raw prices
        monthly = df.resample('ME').agg({
            'Open': 'first',
            'High': 'max',
            'Low': 'min',
            'Close': 'last',
            'Volume': 'sum'
        }).dropna()

        if len(monthly) < 6:
            continue

        # Align with Nifty for Mansfield RS Calculation
        combined = pd.DataFrame({
            'Stock': df['Close'],
            'Nifty': nifty_df['Close']
        }).dropna()
        rel_ratio = combined['Stock'] / combined['Nifty']
        base_ma = rel_ratio.rolling(window=50).mean()
        df['RS'] = ((rel_ratio / base_ma) - 1) * 100

        # Loop through past monthly candles
        for m_idx in range(5, len(monthly) - 1):
            setup_month = monthly.iloc[m_idx]
            prev_month = monthly.iloc[m_idx - 1]
            prev_4_vols = monthly['Volume'].iloc[m_idx - 5 : m_idx]

            cond_close_above_high = setup_month['Close'] > prev_month['High']
            cond_volume_surge = setup_month['Volume'] > prev_4_vols.max()

            m_date = setup_month.name
            if m_date not in df.index:
                continue

            sub_df = df.loc[:m_date]
            if len(sub_df) < 20:
                continue

            latest_rs = sub_df['RS'].iloc[-1]
            recent_lows = sub_df['Low'].iloc[-10:]
            recent_emas = sub_df['20_EMA'].iloc[-10:]
            had_shakeout = any(recent_lows <= recent_emas * 1.01)

            # STRICT BACKTEST CONDITIONS (RS + Shakeout + Volume Surge + Monthly Breakout)
            if cond_close_above_high and cond_volume_surge and (latest_rs > 0) and had_shakeout:
                future_days = df.loc[df.index > m_date]
                if len(future_days) == 0:
                    continue

                entry_date = future_days.index[0]
                entry_price = df.loc[entry_date, 'Open']

                # Stoploss & Targets Calculation
                shakeout_sl = min(setup_month['Low'], sub_df['20_EMA'].iloc[-1] * 0.98)
                stop_loss = round(max(shakeout_sl, entry_price * 0.95), 2)
                target_6_pct = round(entry_price * 1.06, 2)
                target_10_pct = round(entry_price * 1.10, 2)

                # Forward Simulation (Max 30 trading days hold)
                future_df = df.loc[entry_date:].iloc[1:31]
                if future_df.empty:
                    continue

                trade_result = "TIME_EXIT"
                exit_price = future_df['Close'].iloc[-1]
                exit_reason = "Time Exit"

                for f_idx in range(len(future_df)):
                    day_low = future_df['Low'].iloc[f_idx]
                    day_high = future_df['High'].iloc[f_idx]

                    if day_low <= stop_loss:
                        trade_result = "STOP_LOSS"
                        exit_price = stop_loss
                        exit_reason = "SL Hit"
                        break
                    elif day_high >= target_10_pct:
                        trade_result = "WIN_10%"
                        exit_price = target_10_pct
                        exit_reason = "10% Target Hit"
                        break
                    elif day_high >= target_6_pct:
                        trade_result = "WIN_6%"
                        exit_price = target_6_pct
                        exit_reason = "6% Target Hit"
                        break

                pnl_pct = round(((exit_price - entry_price) / entry_price) * 100, 2)
                chart_link_formula = f'=HYPERLINK("https://in.tradingview.com/chart/?symbol=NSE:{stock_clean}", "📊 Open Chart")'

                all_backtest_trades.append([
                    stock_clean,
                    entry_date.strftime('%Y-%m-%d'),
                    entry_price,
                    exit_price,
                    exit_reason,
                    pnl_pct,
                    round(latest_rs, 2),
                    chart_link_formula
                ])

    except Exception as e:
        continue

print(f"📊 Total Backtest Trades Found: {len(all_backtest_trades)}", flush=True)

# 2. Export Backtest Results to Google Sheet (`Backtest_Results` tab)
ws_backtest = get_or_create_worksheet("Backtest_Results")
ws_backtest.clear()

headers = [
    "Stock", 
    "Entry_Date", 
    "Entry_Price", 
    "Exit_Price", 
    "Result", 
    "PnL_Pct", 
    "RS_Score", 
    "TradingView_Chart"
]
ws_backtest.append_row(headers, value_input_option="USER_ENTERED")

if all_backtest_trades:
    ws_backtest.append_rows(sanitize_rows(all_backtest_trades), value_input_option="USER_ENTERED")
    print("✅ Backtest trades successfully exported to Google Sheet tab 'Backtest_Results'!")
else:
    ws_backtest.append_row(["No historical trades matched the strict criteria.", "", "", "", "", "", "", ""], value_input_option="USER_ENTERED")
    print("ℹ️ No historical trades matched the criteria.")
    
