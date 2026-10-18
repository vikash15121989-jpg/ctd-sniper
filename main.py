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
print(f"=== [CTD_SNIPER: HISTORICAL BACKTEST ENGINE] | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

# Google Sheets Connection to read Watchlist
try:
    gcp_json_creds = json.loads(os.environ["GSHEET_KEY"])
    gc = gspread.service_account_from_dict(gcp_json_creds)
    sh = gc.open("CTD_Sniper")
    print("✅ Connected to Google Sheets!", flush=True)
except Exception as e:
    print(f"❌ Connection Error: {e}")
    exit(1)

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
print(f"📥 Backtesting across {len(clean_stocks)} stocks from Watchlist (2 Years History)...\n", flush=True)

all_backtest_trades = []

for symbol in clean_stocks:
    try:
        stock_clean = symbol.replace(".NS", "")
        
        # Fetch 2 years of unadjusted raw historical prices
        data = yf.Ticker(symbol).history(period="2y", interval="1d", auto_adjust=False)
        if data.empty or len(data) < 100:
            continue
        
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)

        # 1. Technical Indicators & Turnover Calculation
        data['Turnover'] = data['Close'] * data['Volume']
        data['Turnover20MA'] = data['Turnover'].rolling(window=20).mean()
        data['MA20'] = data['Close'].rolling(window=20).mean()
        data['MA50'] = data['Close'].rolling(window=50).mean()
        
        # 10-Day Max High & Max Volume (shifted by 1 so today isn't included in the past max)
        data['High_10_Max'] = data['High'].shift(1).rolling(window=10).max()
        data['Vol_10_Max'] = data['Volume'].shift(1).rolling(window=10).max()

        data.dropna(inplace=True)

        # Loop through historical bars (leaving 25 days at the end to evaluate future trades)
        for i in range(50, len(data) - 25):
            row = data.iloc[i]
            
            # Rule 1: Liquidity Check (Avg Daily Turnover >= 3 Cr)
            if row['Turnover20MA'] < 30000000:
                continue
            
            ma20 = row['MA20']
            ma50 = row['MA50']
            if ma20 == 0 or ma50 == 0:
                continue

            # Rule 2: 20 DMA and 50 DMA within 3% range of each other
            ma_gap_pct = abs(ma20 - ma50) / min(ma20, ma50) * 100
            if ma_gap_pct > 3.0:
                continue
                
            # Rule 3: Close price within 5% of both 20 DMA and 50 DMA
            price_to_ma20_pct = abs(row['Close'] - ma20) / ma20 * 100
            price_to_ma50_pct = abs(row['Close'] - ma50) / ma50 * 100
            if price_to_ma20_pct > 5.0 or price_to_ma50_pct > 5.0:
                continue
                
            # Rule 4: Breakout Conditions (Close > 10D High AND Volume > 10D Volume)
            if not (row['Close'] > row['High_10_Max'] and row['Volume'] > row['Vol_10_Max']):
                continue
                
            # Setup Triggered!
            entry_price = row['High']
            stop_loss = row['Low']
            target_6 = entry_price * 1.06
            target_10 = entry_price * 1.10
            sl_risk_pct = (entry_price - stop_loss) / entry_price * 100
            
            # Look forward up to 20 trading days to track outcome
            future_data = data.iloc[i+1 : i+21]
            
            hit_6 = False
            hit_10 = False
            hit_sl = False
            
            first_6_bar = None
            first_10_bar = None
            first_sl_bar = None
            
            for idx_f, (f_date, f_row) in enumerate(future_data.iterrows()):
                if f_row['Low'] <= stop_loss and first_sl_bar is None:
                    first_sl_bar = idx_f
                if f_row['High'] >= target_6 and first_6_bar is None:
                    first_6_bar = idx_f
                if f_row['High'] >= target_10 and first_10_bar is None:
                    first_10_bar = idx_f

            # Determine path-dependent outcome (what happened first)
            success_6 = (first_6_bar is not None) and (first_sl_bar is None or first_6_bar <= first_sl_bar)
            success_10 = (first_10_bar is not None) and (first_sl_bar is None or first_10_bar <= first_sl_bar)
            sl_first = (first_sl_bar is not None) and (first_6_bar is None or first_sl_bar < first_6_bar)

            all_backtest_trades.append({
                'Stock': stock_clean,
                'Date': data.index[i].strftime('%Y-%m-%d'),
                'Entry': round(entry_price, 2),
                'SL': round(stop_loss, 2),
                'SL_Risk_%': round(sl_risk_pct, 2),
                'Hit_6%': success_6,
                'Hit_10%': success_10,
                'Hit_SL_First': sl_first
            })

    except Exception as e:
        continue

# --- Results Compilation & Reporting ---
df_res = pd.DataFrame(all_backtest_trades)

print("=" * 60)
print("📊 BACKTEST PERFORMANCE REPORT")
print("=" * 60)

if not df_res.empty:
    total_trades = len(df_res)
    wins_6 = df_res['Hit_6%'].sum()
    wins_10 = df_res['Hit_10%'].sum()
    sl_hits = df_res['Hit_SL_First'].sum()
    
    print(f"Total Trades Generated    : {total_trades}")
    print(f"Average SL Risk           : {df_res['SL_Risk_%'].mean():.2f}%")
    print("-" * 60)
    print(f"6% Target Winning Rate    : {(wins_6 / total_trades) * 100:.2f}% ({wins_6} / {total_trades})")
    print(f"10% Target Winning Rate   : {(wins_10 / total_trades) * 100:.2f}% ({wins_10} / {total_trades})")
    print(f"SL Hit First Rate         : {(sl_hits / total_trades) * 100:.2f}% ({sl_hits} / {total_trades})")
    print("=" * 60)
    print("\n📋 Sample Trades Breakdown:")
    print(df_res.head(15).to_string(index=False))
else:
    print("❌ No trades matched your strict criteria across the watchlist historical data.")
    print("Tip: Criteria (MA Squeeze within 3% + Price within 5% + Volume/Price Breakout) boht tight hai, aap chahe toh MA gap ko 4-5% kar sakte hain.")
            
