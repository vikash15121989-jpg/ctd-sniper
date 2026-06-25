import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
import time
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== V29.0: 1 YEAR SCAN FROM 25/06/2026 ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== CONFIG - AAJ SE 1 SAAL PEECHE =====
END_DATE = datetime(2026, 6, 25).date() # Aaj
START_DATE = datetime(2025, 6, 25).date() # 1 saal peeche

R = {
    'backtest_start': START_DATE,
    'backtest_end': END_DATE,
    'ema_zone_pct': 0.05,
    'lookback_days': 10,
    'hhhl_days': 20,
}

gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")
ws_output = sh.worksheet("CHoCH_SQUEEZE_SIGNALS")

def get_watchlist_stocks():
    stocks = ws_watchlist.col_values(1)
    stocks = [s.strip().upper() for s in stocks if s.strip() and s.strip().upper() not in ['STOCK', 'SYMBOL', 'NAME']]
    stocks = [s + '.NS' if not s.endswith('.NS') and not s.startswith('^') else s for s in stocks]
    print(f"Watchlist Loaded: {len(stocks)} stocks", flush=True)
    return stocks

def check_valid_choch(df, idx):
    """
    CHoCH Valid = Major Bottom ke baad Higher Low bana AUR CHoCH ke baad Major Bottom kabhi nahi toota
    """
    if idx < 100: return False, None

    hist = df.iloc[:idx+1]
    major_bottom_idx = hist['Low'].idxmin()
    major_bottom_price = hist.loc[major_bottom_idx, 'Low']

    if major_bottom_idx == hist.index[-1]: return False, None

    # Major Lower High
    pre_bottom = hist.loc[:major_bottom_idx]
    if len(pre_bottom) < 10: return False, None
    major_lower_high = pre_bottom['High'].max()
    major_lower_high_idx = pre_bottom['High'].idxmax()

    # CHoCH hua?
    post_lh = hist.loc[hist.index > major_lower_high_idx]
    choch_candle = post_lh[post_lh['Close'] > major_lower_high]
    if choch_candle.empty: return False, None

    choch_date = choch_candle.index[0]

    # CHoCH ke baad Major Bottom toota? Agar toota to CHoCH fail
    post_choch = hist.loc[hist.index > choch_date]
    if post_choch.empty: return False, None
    if post_choch['Low'].min() < major_bottom_price * 0.99:
        return False, None # Naya LL ban gaya, structure fail

    # Higher Low bana?
    higher_low_made = post_choch['Low'].min() > major_bottom_price * 1.01
    return higher_low_made, major_bottom_price

def check_hhhl_now(df, idx):
    if idx < R['hhhl_days']: return False
    recent = df.iloc[idx-R['hhhl_days']+1:idx+1]
    return recent['High'].iloc[-1] >= recent['High'].iloc[0] and recent['Low'].iloc[-1] >= recent['Low'].iloc[0]

def scan_stock(stock, start_date, end_date):
    try:
        time.sleep(1.5)
        # CHoCH ke liye 3 saal purana data chahiye
        df = yf.download(stock, start=start_date - timedelta(days=1100), end=end_date + timedelta(days=1), progress=False, auto_adjust=False)
        if df.empty or len(df) < 150: return []

        df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
        df['10D_High'] = df['High'].rolling(R['lookback_days']).max()
        df['10D_Vol'] = df['Volume'].rolling(R['lookback_days']).max()

        df_scan = df[(df.index >= pd.to_datetime(start_date)) & (df.index <= pd.to_datetime(end_date))]
        signals = []

        for i in range(len(df_scan)):
            idx = df.index.get_loc(df_scan.index[i])
            if idx < 100: continue
            row = df.iloc[idx]

            # FILTER 1: 20 EMA
            if row['Close'] < row['EMA20'] * (1 - R['ema_zone_pct']): continue

            # FILTER 2: VALID CHoCH - No new low after CHoCH
            choch_valid, major_bottom = check_valid_choch(df, idx)
            if not choch_valid: continue

            # FILTER 3: ABHI HH-HL
            if not check_hhhl_now(df, idx): continue

            # FILTER 4: SQUEEZE
            vol_condition = row['Volume'] >= row['10D_Vol']
            price_condition = row['High'] < row['10D_High']
            if not (vol_condition and price_condition): continue

            signal_date = df.index[idx].date()
            signals.append({
                'Signal_Date': str(signal_date),
                'Stock': stock.replace('.NS', ''),
                'Close': round(row['Close'], 2),
                'EMA20': round(row['EMA20'], 2),
                'Major_Bottom': round(major_bottom, 2),
                'Volume': int(row['Volume']),
                '10D_Max_Vol': int(row['10D_Vol']),
                'High': round(row['High'], 2),
                '10D_Max_High': round(row['10D_High'], 2)
            })
            print(f"SIGNAL: {stock} {signal_date} Bottom:{major_bottom:.0f}", flush=True)

        return signals
    except Exception as e:
        print(f"{stock}: {e}", flush=True)
        return []

# MAIN
stocks = get_watchlist_stocks()
all_signals = []

print(f"\n=== SCANNING {len(stocks)} STOCKS FROM {START_DATE} TO {END_DATE} ===", flush=True)
print("Logic: Valid CHoCH Ever + No New Low + HH-HL Now + 20EMA + Squeeze", flush=True)

for i, stock in enumerate(stocks):
    print(f"[{i+1}/{len(stocks)}] {stock}...", flush=True)
    result = scan_stock(stock, START_DATE, END_DATE)
    all_signals.extend(result)
    time.sleep(2)

ws_output.clear()
if all_signals:
    df_final = pd.DataFrame(all_signals).drop_duplicates(subset=['Signal_Date', 'Stock']).sort_values('Signal_Date', ascending=False)
    ws_output.update('A1', [df_final.columns.tolist()] + df_final.values.tolist())
    print(f"\n=== FOUND {len(df_final)} SIGNALS IN LAST 1 YEAR ===", flush=True)
    print(f"Latest 5: \n{df_final.head()}", flush=True)
else:
    ws_output.update('A1', [['No Signals in Last 1 Year']])
    print(f"\n=== 0 SIGNALS ===", flush=True)
