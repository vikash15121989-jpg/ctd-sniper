import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== V143.0 PRODUCTION: LIVE SWEEP SIGNAL SCANNER ===", flush=True)

# ===== CONFIGURATION =====
MIN_AVG_VOLUME = 1_000_000       # 10 Lakh Daily Avg Volume
MIN_AVG_TURNOVER_CR = 5.0        # ₹5 Crore Daily Turnover

END_DATE = datetime.now().date()
START_DATE = END_DATE - timedelta(days=200)

# ===== GOOGLE SHEETS SETUP =====
try:
    gcp_json_creds = json.loads(os.environ["GSHEET_KEY"])
    gc = gspread.service_account_from_dict(gcp_json_creds)
    sh = gc.open("CTD_Sniper")
    ws_watchlist = sh.worksheet("Watchlist")

    try:
        ws_signals = sh.worksheet("Sweep_Signals")
    except Exception:
        ws_signals = sh.add_worksheet(title="Sweep_Signals", rows="100", cols="10")

    raw_stocks = ws_watchlist.col_values(1)
    STOCKS = []
    REJECT_KEYWORDS = ['LIQUID', 'ETF', 'CPSE', 'NETF', 'GILT', 'GOLD', 'SILVER']

    for s in raw_stocks:
        clean_s = s.strip().upper()
        if clean_s and clean_s not in ["STOCK", "SYMBOL", "NAME", "STOCKS"]:
            if not any(k in clean_s for k in REJECT_KEYWORDS):
                if not clean_s.endswith(".NS") and not clean_s.startswith("^"):
                    clean_s += ".NS"
                STOCKS.append(clean_s)

    STOCKS = sorted(list(set(STOCKS)))
    print(f"✅ Loaded {len(STOCKS)} valid stocks for daily scanning.", flush=True)

except Exception as e:
    print(f"❌ Error setting up Google Sheet: {e}")
    exit(1)

# ===== DAILY SCAN ENGINE (V143.0 LOGIC) =====
today_signals = []

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 50:
            continue

        df['Turnover'] = df['Close'] * df['Volume']
        df['Vol_Avg_20'] = df['Volume'].rolling(20).mean()
        df['Turnover_Avg_20_Cr'] = df['Turnover'].rolling(20).mean() / 10_000_000
        df['EMA_20'] = df['Close'].ewm(span=20, adjust=False).mean()

        # Latest Candle Data
        curr = df.iloc[-1]
        prev_10_low = df['Low'].iloc[-11:-1].min()
        prev_10_high = df['High'].iloc[-11:-1].max()

        if curr['Vol_Avg_20'] >= MIN_AVG_VOLUME and curr['Turnover_Avg_20_Cr'] >= MIN_AVG_TURNOVER_CR:
            
            above_20_ema = curr['Close'] > curr['EMA_20']
            swept_low = curr['Low'] < prev_10_low
            closed_above_low = curr['Close'] > prev_10_low
            
            candle_range = curr['High'] - curr['Low']
            strong_rejection = False
            if candle_range > 0:
                close_pos = (curr['Close'] - curr['Low']) / candle_range
                strong_rejection = close_pos >= 0.50

            high_vol = curr['Volume'] >= (1.2 * curr['Vol_Avg_20'])

            # Signal Trigger
            if above_20_ema and swept_low and closed_above_low and strong_rejection and high_vol:
                entry = round(curr['Close'], 2)
                sl = round(curr['Low'] * 0.995, 2)
                risk = entry - sl
                
                min_target = entry + (1.8 * risk)
                target = round(max(prev_10_high, min_target), 2)
                
                risk_pct = round((risk / entry) * 100, 2)
                reward_pct = round(((target - entry) / entry) * 100, 2)

                if risk_pct <= 7.0:
                    today_signals.append([
                        datetime.now().strftime("%Y-%m-%d"),
                        stock.replace(".NS", ""),
                        entry,
                        sl,
                        target,
                        f"{risk_pct}%",
                        f"+{reward_pct}%",
                        round(curr['Turnover_Avg_20_Cr'], 2)
                    ])

    except Exception:
        pass

# ===== UPDATE GOOGLE SHEET =====
headers = ["Date", "Symbol", "Entry Price", "Stop Loss", "Target", "Risk %", "Expected Return %", "Avg Turnover (Cr)"]
ws_signals.clear()
ws_signals.append_row(headers)

if today_signals:
    ws_signals.append_rows(today_signals)
    print(f"\n🚀 SCAN COMPLETE: Found {len(today_signals)} active Sweep Signals!", flush=True)
else:
    print("\n⚡ SCAN COMPLETE: No active sweep signals today.", flush=True)
