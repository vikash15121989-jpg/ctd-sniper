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
print(f"=== [LIVE CTD_SNIPER: DAILY BREAKOUT & MA SQUEEZE SCANNER] | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

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

# 1. Read Watchlist from Sheet
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
print(f"📥 Scanning {len(clean_stocks)} stocks with unadjusted raw prices...", flush=True)

live_signals = []

for symbol in clean_stocks:
    try:
        stock_clean = symbol.replace(".NS", "")
        
        # Auto-adjust False for raw prices matching trading charts
        data = yf.Ticker(symbol).history(period="1y", interval="1d", auto_adjust=False)
        if data.empty or len(data) < 60:
            continue
        
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)

        # 1. Daily Liquidity Check (>= ₹3 Cr Avg Daily Turnover over last 20 days)
        data['Turnover'] = data['Close'] * data['Volume']
        recent_daily_turnover = data['Turnover'].iloc[-20:].mean()
        
        if recent_daily_turnover < 30000000: # ₹3 Crore threshold
            continue

        # Moving Averages calculation
        data['MA20'] = data['Close'].rolling(window=20).mean()
        data['MA50'] = data['Close'].rolling(window=50).mean()

        # 10 Days Max High & Max Volume (excluding today's running/breakout candle)
        data['High_10_Max'] = data['High'].shift(1).rolling(window=10).max()
        data['Vol_10_Max'] = data['Volume'].shift(1).rolling(window=10).max()

        latest_bar = data.iloc[-1]
        
        # 2. Moving Average Closeness Check (20 DMA & 50 DMA within 3%)
        ma20 = latest_bar['MA20']
        ma50 = latest_bar['MA50']
        if pd.isna(ma20) or pd.isna(ma50) or min(ma20, ma50) == 0:
            continue
            
        ma_gap_pct = abs(ma20 - ma50) / min(ma20, ma50) * 100
        cond_ma_close = ma_gap_pct <= 3.0

        # 3. Price Proximity Check (Price within 5% of both 20 DMA & 50 DMA)
        price_to_ma20_pct = abs(latest_bar['Close'] - ma20) / ma20 * 100
        price_to_ma50_pct = abs(latest_bar['Close'] - ma50) / ma50 * 100
        cond_price_near_ma = (price_to_ma20_pct <= 5.0) and (price_to_ma50_pct <= 5.0)

        # 4. Breakout Conditions
        cond_price_breakout = latest_bar['Close'] > latest_bar['High_10_Max']
        cond_volume_breakout = latest_bar['Volume'] > latest_bar['Vol_10_Max']

        # Evaluate All Logic Criteria Together
        if cond_ma_close and cond_price_near_ma and cond_price_breakout and cond_volume_breakout:
            entry_level = round(latest_bar['High'], 2)              # Entry at High of breakout candle
            stoploss_level = round(latest_bar['Low'], 2)            # Tight StopLoss at Low of breakout candle
            target_6_level = round(entry_level * 1.06, 2)            # 6% Target
            target_10_level = round(entry_level * 1.10, 2)           # 10% Target
            
            chart_link_formula = f'=HYPERLINK("https://in.tradingview.com/chart/?symbol=NSE:{stock_clean}", "📊 Open Chart")'
            
            live_signals.append([
                stock_clean,
                latest_bar.name.strftime('%Y-%m-%d'),
                entry_level,
                target_6_level,
                target_10_level,
                stoploss_level,
                round(latest_bar['Close'], 2),
                round(latest_bar['High_10_Max'], 2),
                round(recent_daily_turnover / 10000000, 2),
                chart_link_formula
            ])

    except Exception as e:
        continue

print(f"🚀 Valid Breakouts Found Today: {len(live_signals)}", flush=True)

# 2. Export Live Signals to Google Sheet
ws_live = get_or_create_worksheet("Live_Breakout_Signals")
ws_live.clear()

headers = [
    "Stock", 
    "Breakout_Date", 
    "Entry_Trigger_(High)", 
    "Target_6%", 
    "Target_10%", 
    "StopLoss_(Low)", 
    "Breakout_Close", 
    "10D_Max_High", 
    "Avg_Daily_Turnover_Cr", 
    "TradingView_Chart"
]

ws_live.append_row(headers, value_input_option="USER_ENTERED")

if live_signals:
    ws_live.append_rows(sanitize_rows(live_signals), value_input_option="USER_ENTERED")
    print("✅ Signals successfully exported using new breakout logic!")
else:
    ws_live.append_row(["No stocks matching strict criteria today.", "", "", "", "", "", "", "", "", ""], value_input_option="USER_ENTERED")
    print("ℹ️ No stocks matched the criteria today.")
    
