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
print(f"=== [LIVE CTD_SNIPER: COMPLETED MONTH BREAKOUT SCANNER] | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

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
print(f"📥 Scanning {len(clean_stocks)} stocks using completed closed months...", flush=True)

live_signals = []

for symbol in clean_stocks:
    try:
        stock_clean = symbol.replace(".NS", "")
        data = yf.Ticker(symbol).history(period="2y", interval="1d")
        if data.empty or len(data) < 200:
            continue
        
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)

        # 1. Daily Liquidity Check (>= ₹2 Cr Avg Daily Turnover)
        data['Turnover'] = data['Close'] * data['Volume']
        recent_daily_turnover = data['Turnover'].iloc[-20:].mean()
        
        if recent_daily_turnover < 20000000:
            continue

        # Resample to Monthly
        monthly = data.resample('ME').agg({
            'Open': 'first',
            'High': 'max',
            'Low': 'min',
            'Close': 'last',
            'Volume': 'sum'
        }).dropna()

        if len(monthly) < 7:
            continue

        # INDEX FIX: 
        # index[-1] is the current running/incomplete month -> IGNORE IT!
        # index[-2] is the last fully completed/closed month -> Setup Month
        # index[-3] is the month before setup -> Previous Month
        
        prev_month = monthly.iloc[-3]    
        setup_month = monthly.iloc[-2]     
        
        # 2. Monthly Breakout Conditions on Completed Month
        cond_close_above_high = setup_month['Close'] > prev_month['High']
        prev_4_vols = monthly['Volume'].iloc[-6:-2]  # 4 completed months prior to setup month
        cond_volume_surge = setup_month['Volume'] > prev_4_vols.max()
        
        if cond_close_above_high and cond_volume_surge:
            entry_level = round(setup_month['High'], 2)
            stoploss_level = round(setup_month['Low'], 2)
            target_level = round(entry_level * 1.06, 2)
            
            chart_link_formula = f'=HYPERLINK("https://in.tradingview.com/chart/?symbol=NSE:{stock_clean}", "📊 Open Chart")'
            
            live_signals.append([
                stock_clean,
                setup_month.name.strftime('%Y-%m'),  # Now shows the correct completed past month
                entry_level,
                target_level,
                stoploss_level,
                round(setup_month['Close'], 2),
                round(recent_daily_turnover / 10000000, 2),
                chart_link_formula
            ])

    except Exception:
        continue

print(f"🚀 Live Breakouts Found on Completed Months: {len(live_signals)}", flush=True)

# 2. Export Live Signals to Google Sheet ("Live_Breakout_Signals")
ws_live = get_or_create_worksheet("Live_Breakout_Signals")
ws_live.clear()

headers = ["Stock", "Setup_Month", "Entry_Trigger", "Target_6%", "StopLoss", "Setup_Close", "Avg_Daily_Turnover_Cr", "TradingView_Chart"]
ws_live.append_row(headers, value_input_option="USER_ENTERED")

if live_signals:
    ws_live.append_rows(sanitize_rows(live_signals), value_input_option="USER_ENTERED")
    print("✅ Corrected signals with completed months exported successfully!")
else:
    ws_live.append_row(["No stocks matching setup on completed months today.", "", "", "", "", "", "", ""], value_input_option="USER_ENTERED")
    print("ℹ️ No stocks matched the criteria.")
    
