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
print(f"=== [WEEKLY SHAKEOUT -> NEXT WEEK DAILY MAX VOL BREAKOUT] | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

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

# Read Watchlist
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
print(f"📥 Loaded {len(clean_stocks)} stocks from Watchlist...", flush=True)

all_backtest_trades = []
TOTAL_CAPITAL = 1000000  # 10 Lakh Capital
RISK_PER_TRADE_PCT = 0.01  # 1% Risk per trade
allowed_rupee_risk = TOTAL_CAPITAL * RISK_PER_TRADE_PCT

for symbol in clean_stocks:
    try:
        stock_clean = symbol.replace(".NS", "")
        df = yf.download(symbol, start="2024-01-01", end="2026-10-01", auto_adjust=False, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 120:
            continue

        df['Turnover'] = df['Close'] * df['Volume']

        # Weekly Resample Data
        weekly_df = df.resample('W').agg({
            'Open': 'first',
            'High': 'max',
            'Low': 'min',
            'Close': 'last',
            'Volume': 'sum'
        }).dropna()
        
        weekly_df['20_EMA'] = weekly_df['Close'].ewm(span=20, adjust=False).mean()

        i = 60
        while i < len(df) - 15:
            hist_daily = df.iloc[:i+1]
            current_date = df.index[i]
            
            # Liquidity Filter (Min 15 Cr turnover)
            avg_turnover = hist_daily['Turnover'].iloc[-20:].mean()
            if avg_turnover < 150000000:
                i += 1
                continue

            # Get weekly data up to current date
            hist_weekly = weekly_df[weekly_df.index <= current_date]
            if len(hist_weekly) < 15:
                i += 1
                continue

            # 1. Check if PREVIOUS week had a Shakeout (Low < 20 EMA)
            prev_week = hist_weekly.iloc[-2]
            is_prev_week_shakeout = prev_week['Low'] < prev_week['20_EMA']

            if not is_prev_week_shakeout:
                i += 1
                continue

            # 2. In the current daily timeframe (following week/days):
            box_slice = hist_daily.iloc[-11:-1]
            prev_10_max_vol = box_slice['Volume'].max()
            prev_10_max_high = box_slice['High'].max()
            prev_10_min_low = box_slice['Low'].min()

            latest_vol = hist_daily['Volume'].iloc[-1]
            latest_close = hist_daily['Close'].iloc[-1]

            # Volume greater than 10-day max volume AND Breakout above 10-day max high
            has_max_vol = latest_vol > prev_10_max_vol
            is_breakout = latest_close > prev_10_max_high

            if has_max_vol and is_breakout:
                entry_date = current_date
                entry_price = latest_close
                
                # Stop-Loss: 10-day box low
                stop_loss = round(prev_10_min_low, 2)
                box_height = entry_price - stop_loss
                
                if box_height <= 0:
                    i += 1
                    continue

                # Target: 1.5x Risk-to-Reward
                target_price = round(entry_price + (1.5 * box_height), 2)
                
                risk_per_share = entry_price - stop_loss
                shares_quantity = int(allowed_rupee_risk / risk_per_share)
                
                # Forward Simulation (Max 15 days time-stop)
                future_df = df.iloc[i+1:]
                if future_df.empty:
                    i += 1
                    continue

                exit_price = 0.0
                exit_reason = ""
                days_in_trade = 0
                max_hold_days = 15

                for f_idx in range(min(max_hold_days, len(future_df))):
                    day_low = future_df['Low'].iloc[f_idx]
                    day_high = future_df['High'].iloc[f_idx]
                    day_close = future_df['Close'].iloc[f_idx]

                    if day_low <= stop_loss:
                        exit_price = stop_loss
                        exit_reason = "SL Hit"
                        days_in_trade = f_idx + 1
                        break
                    elif day_high >= target_price:
                        exit_price = target_price
                        exit_reason = "Target Hit (1.5x)"
                        days_in_trade = f_idx + 1
                        break
                    elif f_idx == max_hold_days - 1:
                        exit_price = day_close
                        exit_reason = "Time Exit"
                        days_in_trade = f_idx + 1
                        break

                if exit_price > 0:
                    pnl_pct = round(((exit_price - entry_price) / entry_price) * 100, 2)
                    trade_rupee_pnl = round(shares_quantity * (exit_price - entry_price), 2)
                    chart_link = f'=HYPERLINK("https://in.tradingview.com/chart/?symbol=NSE:{stock_clean}", "📊 Chart")'

                    all_backtest_trades.append([
                        stock_clean,
                        entry_date.strftime('%Y-%m-%d'),
                        round(entry_price, 2),
                        round(stop_loss, 2),
                        round(target_price, 2),
                        round(exit_price, 2),
                        shares_quantity,
                        exit_reason,
                        days_in_trade,
                        pnl_pct,
                        trade_rupee_pnl,
                        round((box_height/entry_price)*100, 2),
                        chart_link
                    ])
                    i += days_in_trade
                else:
                    i += 1
            else:
                i += 1
    except Exception as e:
        continue

# Summary Calculations
total_trades = len(all_backtest_trades)
if total_trades > 0:
    df_results = pd.DataFrame(all_backtest_trades, columns=[
        "Stock", "Entry_Date", "Entry_Price", "StopLoss", "Target_Price", "Exit_Price", 
        "Quantity", "Result", "Days_Held", "PnL_Pct", "Rupee_PnL", "Risk_Pct", "TradingView_Chart"
    ])
    
    winning_trades = len(df_results[df_results['PnL_Pct'] > 0])
    losing_trades = len(df_results[df_results['PnL_Pct'] <= 0])
    win_rate = round((winning_trades / total_trades) * 100, 2)
    total_pnl_pct = round(df_results['PnL_Pct'].sum(), 2)
    avg_pnl_pct = round(df_results['PnL_Pct'].mean(), 2)
    total_rupee_pnl = round(df_results['Rupee_PnL'].sum(), 2)

    print("\n" + "=" * 50)
    print("   WEEKLY SHAKEOUT -> DAILY MAX VOL + BREAKOUT RESULTS")
    print("=" * 50)
    print(f"Total Trades Taken     : {total_trades}")
    print(f"Winning Trades         : {winning_trades}")
    print(f"Losing Trades          : {losing_trades}")
    print(f"Win Rate (%)           : {win_rate}%")
    print(f"Total Cumulative PnL   : {total_pnl_pct}%")
    print(f"Average PnL per Trade  : {avg_pnl_pct}%")
    print(f"Total Rupee PnL        : ₹{total_rupee_pnl}")
    print("=" * 50 + "\n")

# Export to Google Sheets
ws_backtest = get_or_create_worksheet("Backtest_Results")
ws_backtest.clear()

if total_trades > 0:
    summary_header = [
        f"Total Trades: {total_trades}", 
        f"Win Rate: {win_rate}%", 
        f"Winning: {winning_trades}", 
        f"Losing: {losing_trades}", 
        f"Total PnL: {total_pnl_pct}%", 
        f"Avg PnL: {avg_pnl_pct}%", 
        f"Total Rupee PnL: ₹{total_rupee_pnl}", "", "", "", "", "", ""
    ]
    ws_backtest.append_row(summary_header, value_input_option="USER_ENTERED")
    ws_backtest.append_row([], value_input_option="USER_ENTERED")

    headers = [
        "Stock", "Entry_Date", "Entry_Price", "StopLoss", "Target_Price", "Exit_Price", 
        "Quantity", "Result", "Days_Held", "PnL_Pct", "Rupee_PnL", "Risk_Pct", "TradingView_Chart"
    ]
    ws_backtest.append_row(headers, value_input_option="USER_ENTERED")
    ws_backtest.append_rows(sanitize_rows(all_backtest_trades), value_input_option="USER_ENTERED")
    print("✅ Backtest results successfully exported to Google Sheet!")
    
