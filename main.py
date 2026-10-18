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
                
                # PULLBACK & RE-TEST ENTRY LOGIC:
                # Look into the next 10 days for a dip/pullback followed by a bounce above previous high
                forward_window = df.iloc[i+1 : i+11]
                entry_triggered = False
                entry_date = None
                entry_price = 0.0

                has_pulled_back = False
                for f_idx in range(len(forward_window)):
                    bar = forward_window.iloc[f_idx]
                    
                    # Pullback condition: price dips below breakout close or towards 20 EMA
                    if bar['Low'] <= bar['20_EMA'] or bar['Close'] < latest_close:
                        has_pulled_back = True

                    # Entry trigger: after a pullback, price crosses above the previous day's high
                    if has_pulled_back and f_idx > 0:
                        prev_bar = forward_window.iloc[f_idx - 1]
                        if bar['High'] > prev_bar['High']:
                            entry_date = forward_window.index[f_idx]
                            entry_price = prev_bar['High']  # Buy stop at prev high
                            entry_triggered = True
                            entry_idx_in_df = df.index.get_loc(entry_date)
                            break

                if entry_triggered:
                    stop_loss = round(min(shakeout_low, entry_price * 0.95), 2)  # Max 5% SL
                    risk_per_share = entry_price - stop_loss

                    if risk_per_share
                    
