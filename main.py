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
print(f"=== [EMA PULLBACK + BULLISH CANDLESTICK BACKTEST] | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

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

# Fetch Nifty Data for RS calculation
try:
    nifty_df = yf.download("^NSEI", start="2024-01-01", end="2026-10-01", auto_adjust=False, progress=False)
    if isinstance(nifty_df.columns, pd.MultiIndex):
        nifty_df = nifty_df.xs("^NSEI", level=1, axis=1)
except Exception as e:
    print(f"❌ Nifty Fetch Error: {e}")
    exit(1)

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

        df['20_EMA'] = df['Close'].ewm(span=20, adjust=False).mean()
        df['50_EMA'] = df['Close'].ewm(span=50, adjust=False).mean()
        df['Turnover'] = df['Close'] * df['Volume']

        # Relative Strength calculation
        combined = pd.DataFrame({'Stock': df['Close'], 'Nifty': nifty_df['Close']}).dropna()
        rel_ratio = combined['Stock'] / combined['Nifty']
        base_ma = rel_ratio.rolling(window=50).mean()
        df['RS'] = ((rel_ratio / base_ma) - 1) * 100
        df = df.reindex(combined.index).dropna()

        i = 60
        while i < len(df) - 15:
            hist_daily = df.iloc[:i+1]
            current_date = df.index[i]
            
            # Liquidity Filter: Min ₹8 Cr to Max ₹25 Cr Daily Turnover
            avg_turnover = hist_daily['Turnover'].iloc[-20:].mean()
            if avg_turnover < 80000000 or avg_turnover > 250000000:
                i += 1
                continue

            # Check Relative Strength (RS >= 10.0)
            latest_rs = hist_daily['RS'].iloc[-1]
            if latest_rs < 10.0:
                i += 1
                continue

            # Current and previous candle data for pattern identification
            curr = hist_daily.iloc[-1]
            prev = hist_daily.iloc[-2]

            c_open, c_high, c_low, c_close = curr['Open'], curr['High'], curr['Low'], curr['Close']
            p_open, p_close = prev['Open'], prev['Close']
            ema_20 = curr['20_EMA']
            ema_50 = curr['50_EMA']

            # 1. Pullback Condition: Price is near 20 EMA or 50 EMA (within 2% range)
            near_20ema = abs(c_close - ema_20) / ema_20 <= 0.02
            near_50ema = abs(c_close - ema_50) / ema_50 <= 0.02
            is_pullback = near_20ema or near_50ema

            if not is_pullback:
                i += 1
                continue

            # 2. Bullish Candlestick Pattern Identification
            body = abs(c_close - c_open)
            total_range = c_high - c_low
            if total_range == 0:
                i += 1
                continue

            lower_wick = min(c_open, c_close) - c_low
            upper_wick = c_high - max(c_open, c_close)

            # Hammer Definition: Small body at the top, long lower wick (at least 2x body), short upper wick
            is_hammer = (c_close > c_open) and (lower_wick >= 2 * body) and (upper_wick < body * 0.5)

            # Bullish Engulfing Definition: Previous candle was red, current candle is green and completely engulfs previous body
            is_engulfing = (p_close < p_open) and (c_close > c_open) and (c_open <= p_close) and (c_close >= p_open)

            if is_hammer or is_engulfing:
                entry_date = current_date
                entry_price = c_close
                
                # Stop-Loss: Low of the signal candle
                stop_loss = round(c_low, 2)
                risk_per_share = entry_price - stop_loss
                
                if risk_per_share <= 0:
                    i += 1
                    continue

                # Target: Minimum 10% target or 2x Risk-to-Reward
                target_price = round(max(entry_price * 1.10, entry_price + (2.0 * risk_per_share)), 2)
                
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
                        exit_reason = "Target Hit (10%+)"
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
                    pattern_name = "Hammer" if is_hammer else "Bullish Engulfing"

                    all_backtest_trades.append([
                        stock_clean,
                        entry_date.strftime('%Y-%m-%d'),
                        pattern_name,
                        round(entry_price, 2),
                        round(stop_loss, 2),
                        round(target_price, 2),
                        round(exit_price, 2),
                        shares_quantity,
                        exit_reason,
                        days_in_trade,
                        pnl_pct,
                        trade_rupee_pnl,
                        chart_link
                    ])
                    # Skip ahead to space out trades per stock
                    i += max(days_in_trade, 15)
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
        "Stock", "Entry_Date", "Pattern", "Entry_Price", "StopLoss", "Target_Price", "Exit_Price", 
        "Quantity", "Result", "Days_Held", "PnL_Pct", "Rupee_PnL", "TradingView_Chart"
    ])
    
    winning_trades = len(df_results[df_results['PnL_Pct'] > 0])
    losing_trades = len(df_results[df_results['PnL_Pct'] <= 0])
    win_rate = round((winning_trades / total_trades) * 100, 2)
    total_pnl_pct = round(df_results['PnL_Pct'].sum(), 2)
    avg_pnl_pct = round(df_results['PnL_Pct'].mean(), 2)
    total_rupee_pnl = round(df_results['Rupee_PnL'].sum(), 2)

    print("\n" + "=" * 50)
    print("   EMA PULLBACK + CANDLESTICK BACKTEST RESULTS")
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
        "Stock", "Entry_Date", "Pattern", "Entry_Price", "StopLoss", "Target_Price", "Exit_Price", 
        "Quantity", "Result", "Days_Held", "PnL_Pct", "Rupee_PnL", "TradingView_Chart"
    ]
    ws_backtest.append_row(headers, value_input_option="USER_ENTERED")
    ws_backtest.append_rows(sanitize_rows(all_backtest_trades), value_input_option="USER_ENTERED")
    print("✅ EMA Pullback candlestick backtest results successfully exported to Google Sheet!")
    
