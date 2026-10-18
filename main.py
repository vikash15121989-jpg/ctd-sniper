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
print(f"=== [CTD_SNIPER: PULLBACK & RETEST ENTRY BACKTEST] | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

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
print(f"📥 Loaded {len(clean_stocks)} stocks from Google Sheet Watchlist...", flush=True)

# Fetch Benchmark Nifty Data for Mansfield RS Calculation
try:
    nifty_df = yf.download("^NSEI", start="2024-01-01", end="2026-10-01", auto_adjust=False, progress=False)
    if isinstance(nifty_df.columns, pd.MultiIndex):
        nifty_df = nifty_df.xs("^NSEI", level=1, axis=1)
    nifty_df['20_EMA'] = nifty_df['Close'].ewm(span=20, adjust=False).mean()
except Exception as e:
    print(f"❌ Nifty Benchmark Fetch Error: {e}")
    exit(1)

all_backtest_trades = []

TOTAL_CAPITAL = 1000000  # 10 Lakh Capital
RISK_PER_TRADE_PCT = 0.01  # 1% Risk per trade
allowed_rupee_risk = TOTAL_CAPITAL * RISK_PER_TRADE_PCT

for symbol in clean_stocks:
    try:
        stock_clean = symbol.replace(".NS", "")
        print(f"Scanning & Backtesting: {stock_clean}...", flush=True)
        
        df = yf.download(symbol, start="2024-01-01", end="2026-10-01", auto_adjust=False, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 150:
            continue

        df['20_EMA'] = df['Close'].ewm(span=20, adjust=False).mean()
        df['50_EMA'] = df['Close'].ewm(span=50, adjust=False).mean()
        df['Vol_20_MA'] = df['Volume'].rolling(window=20).mean()
        df['Turnover'] = df['Close'] * df['Volume']

        combined = pd.DataFrame({
            'Stock': df['Close'],
            'Nifty': nifty_df['Close']
        }).dropna()
        rel_ratio = combined['Stock'] / combined['Nifty']
        base_ma = rel_ratio.rolling(window=50).mean()
        df['RS'] = ((rel_ratio / base_ma) - 1) * 100
        df = df.reindex(combined.index).dropna()

        i = 60
        while i < len(df) - 40:
            hist = df.iloc[:i+1]
            
            latest_close = hist['Close'].iloc[-1]
            latest_20_ema = hist['20_EMA'].iloc[-1]
            latest_50_ema = hist['50_EMA'].iloc[-1]
            latest_vol = hist['Volume'].iloc[-1]
            avg_vol_20 = hist['Vol_20_MA'].iloc[-1]
            latest_rs = hist['RS'].iloc[-1]
            avg_turnover = hist['Turnover'].iloc[-20:].mean()

            # 1. LIQUIDITY FILTER: Minimum 10 Crore Daily Turnover
            if avg_turnover < 100000000:
                i += 1
                continue

            # 2. Trend Filter: 20 EMA > 50 EMA and Price above 20 EMA
            is_uptrend = (latest_20_ema > latest_50_ema) and (latest_close > latest_20_ema)

            # 3. Shakeout Check: Price dipped below 20 EMA within last 12 days and reclaimed
            recent_lows = hist['Low'].iloc[-12:-1]
            recent_20_emas = hist['20_EMA'].iloc[-12:-1]
            had_shakeout = any(recent_lows < recent_20_emas)

            # 4. Breakout Setup Condition: Volume > 10-Day Max Volume & CLOSE > 10-DAY MAX HIGH
            prev_10_max_vol = hist['Volume'].iloc[-11:-1].max()
            prev_10_max_high = hist['High'].iloc[-11:-1].max()

            is_volume_spike = (latest_vol > prev_10_max_vol) and (latest_vol >= 2.0 * avg_vol_20)
            is_close_above_max_high = latest_close > prev_10_max_high

            # 5. MANSFIELD RS >= 10.0
            is_rs_elite = latest_rs >= 10.0

            if is_uptrend and had_shakeout and is_volume_spike and is_close_above_max_high and is_rs_elite:
                shakeout_low = hist['Low'].iloc[-12:].min()
                
                # PULLBACK & RE-TEST ENTRY LOGIC
                forward_window = df.iloc[i+1 : i+11]
                entry_triggered = False
                entry_date = None
                entry_price = 0.0

                has_pulled_back = False
                for f_idx in range(len(forward_window)):
                    bar = forward_window.iloc[f_idx]
                    
                    if bar['Low'] <= bar['20_EMA'] or bar['Close'] < latest_close:
                        has_pulled_back = True

                    if has_pulled_back and f_idx > 0:
                        prev_bar = forward_window.iloc[f_idx - 1]
                        if bar['High'] > prev_bar['High']:
                            entry_date = forward_window.index[f_idx]
                            entry_price = prev_bar['High']
                            entry_triggered = True
                            entry_idx_in_df = df.index.get_loc(entry_date)
                            break

                if entry_triggered:
                    stop_loss = round(min(shakeout_low, entry_price * 0.95), 2)  # Max 5% SL
                    risk_per_share = entry_price - stop_loss

                    if risk_per_share <= 0:
                        i += 1
                        continue

                    shares_quantity = int(allowed_rupee_risk / risk_per_share)
                    target_price = round(entry_price + (2 * risk_per_share), 2)  # 2x Risk Target

                    future_df = df.iloc[entry_idx_in_df + 1 : entry_idx_in_df + 31]
                    if future_df.empty:
                        i += 1
                        continue

                    exit_price = future_df['Close'].iloc[-1]
                    exit_reason = "Time Exit"

                    for f_idx in range(len(future_df)):
                        day_low = future_df['Low'].iloc[f_idx]
                        day_high = future_df['High'].iloc[f_idx]

                        if day_low <= stop_loss:
                            exit_price = stop_loss
                            exit_reason = "SL Hit"
                            break
                        elif day_high >= target_price:
                            exit_price = target_price
                            exit_reason = "Target Hit (2x Risk)"
                            break

                    pnl_pct = round(((exit_price - entry_price) / entry_price) * 100, 2)
                    trade_rupee_pnl = round(shares_quantity * (exit_price - entry_price), 2)
                    chart_link_formula = f'=HYPERLINK("https://in.tradingview.com/chart/?symbol=NSE:{stock_clean}", "📊 Open Chart")'

                    all_backtest_trades.append([
                        stock_clean,
                        entry_date.strftime('%Y-%m-%d'),
                        round(entry_price, 2),
                        round(stop_loss, 2),
                        round(target_price, 2),
                        round(exit_price, 2),
                        shares_quantity,
                        exit_reason,
                        pnl_pct,
                        trade_rupee_pnl,
                        round(latest_rs, 2),
                        chart_link_formula
                    ])
                    
                    i = entry_idx_in_df + 60
                else:
                    i += 10
            else:
                i += 1

    except Exception as e:
        continue

# 2. Calculate Performance Metrics
total_trades = len(all_backtest_trades)
if total_trades > 0:
    df_results = pd.DataFrame(all_backtest_trades, columns=[
        "Stock", "Entry_Date", "Entry_Price", "StopLoss", "Target_Price", "Exit_Price", 
        "Quantity", "Result", "PnL_Pct", "Rupee_PnL", "RS_Score", "TradingView_Chart"
    ])
    
    winning_trades = len(df_results[df_results['PnL_Pct'] > 0])
    losing_trades = len(df_results[df_results['PnL_Pct'] <= 0])
    win_rate = round((winning_trades / total_trades) * 100, 2)
    total_pnl_pct = round(df_results['PnL_Pct'].sum(), 2)
    avg_pnl_pct = round(df_results['PnL_Pct'].mean(), 2)
    total_rupee_pnl = round(df_results['Rupee_PnL'].sum(), 2)

    print("\n" + "=" * 50)
    print("     PULLBACK & RETEST BACKTEST SUMMARY (RS >= 10)")
    print("=" * 50)
    print(f"Total Trades Taken     : {total_trades}")
    print(f"Winning Trades         : {winning_trades}")
    print(f"Losing Trades          : {losing_trades}")
    print(f"Win Rate (%)           : {win_rate}%")
    print(f"Total Cumulative PnL   : {total_pnl_pct}%")
    print(f"Average PnL per Trade  : {avg_pnl_pct}%")
    print(f"Total Rupee PnL        : ₹{total_rupee_pnl}")
    print("=" * 50 + "\n")
else:
    winning_trades = 0
    losing_trades = 0
    win_rate = 0.0
    total_pnl_pct = 0.0
    avg_pnl_pct = 0.0

# 3. Export to Google Sheet (`Backtest_Results` tab)
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
        f"Total Rupee PnL: ₹{total_rupee_pnl}", "", "", "", "", ""
    ]
    ws_backtest.append_row(summary_header, value_input_option="USER_ENTERED")
    ws_backtest.append_row([], value_input_option="USER_ENTERED")

    headers = [
        "Stock", "Entry_Date", "Entry_Price", "StopLoss", "Target_Price", "Exit_Price", 
        "Quantity", "Result", "PnL_Pct", "Rupee_PnL", "RS_Score", "TradingView_Chart"
    ]
    ws_backtest.append_row(headers, value_input_option="USER_ENTERED")
    ws_backtest.append_rows(sanitize_rows(all_backtest_trades), value_input_option="USER_ENTERED")
    print("✅ Pullback & Retest results successfully exported to Google Sheet!")
else:
    ws_backtest.append_row(["No historical trades matched the criteria.", "", "", "", "", "", "", "", "", "", "", ""], value_input_option="USER_ENTERED")
    print("ℹ️ No historical trades matched the criteria.")
    
