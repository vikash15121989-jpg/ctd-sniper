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

print("=== V35.1: PRE-BREAKOUT SQUEEZE SCANNER - OPTION 2 ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== CONFIG =====
END_DATE = datetime(2026, 6, 25).date()
START_DATE = datetime(2025, 6, 25).date()
BATCH_SIZE = 15

R = {
    'backtest_start': START_DATE,
    'backtest_end': END_DATE,
    'lookback_days': 14, # RANGE CHECK PERIOD
}

gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

try:
    ws_output = sh.worksheet("CHoCH_SQUEEZE_SIGNALS")
    print("Worksheet connected.", flush=True)
except gspread.exceptions.WorksheetNotFound:
    ws_output = sh.add_worksheet(title="CHoCH_SQUEEZE_SIGNALS", rows="1000", cols="12")
    print("Worksheet created automatically!", flush=True)

def get_watchlist_stocks():
    stocks = ws_watchlist.col_values(1)
    stocks = [s.strip().upper() for s in stocks if s.strip() and s.strip().upper() not in ['STOCK', 'SYMBOL', 'NAME']]
    stocks = [s + '.NS' if not s.endswith('.NS') and not s.startswith('^') else s for s in stocks]
    return stocks

def check_structure_and_choch(df, idx, lookback=60):
    if idx < lookback: return False, None
    window = df.iloc[idx-lookback:idx+1]
    local_bottom_idx = window['Low'].idxmin()
    local_bottom_price = window.loc[local_bottom_idx, 'Low']

    current_close = df.iloc[idx]['Close']
    if current_close < local_bottom_price: return False, None

    recent_20 = df.iloc[idx-20:idx+1]
    if recent_20['Low'].min() <= local_bottom_price: return False, None

    return True, local_bottom_price

def process_single_stock_data(stock_df, stock, start_date, end_date):
    signals = []
    try:
        df = stock_df.dropna(subset=['Close']).copy()
        if len(df) < 80: return []

        df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
        df['20_std'] = df['Close'].rolling(window=20).std()
        df['Squeeze_Threshold'] = df['20_std'].rolling(window=50).quantile(0.20)
        df['Avg_Vol'] = df['Volume'].rolling(window=20).mean()

        df_scan = df[(df.index >= pd.to_datetime(start_date)) & (df.index <= pd.to_datetime(end_date))]

        for i in range(len(df_scan)):
            idx = df.index.get_loc(df_scan.index[i])
            if idx < 20: continue

            row = df.iloc[idx]

            # 1. Base Trend: Uptrend में ही होना चाहिए
            if row['Close'] < row['EMA20']: continue

            # 2. Structure: कोई मेजर मंदी न चल रही हो
            struct_valid, recent_bottom = check_structure_and_choch(df, idx)
            if not struct_valid: continue

            # पिछले 14 दिनों की रेंज निकालें
            recent_14 = df.iloc[idx-R['lookback_days']:idx]
            range_high = recent_14['High'].max()
            range_low = recent_14['Low'].min()

            # =======================================================
            # 💎 OPTION 2 LOGIC: SAB SQUEEZE PAKDO, DISTANCE SE SORT KARO
            # =======================================================

            # कंडीशन A: अभी ब्रेकआउट नहीं हुआ होना चाहिए
            has_not_broken_out = row['Close'] <= range_high

            # कंडीशन B: पूरी तरह से स्क्वीज़
            is_squeezed = row['20_std'] <= row['Squeeze_Threshold']

            # कंडीशन C: वॉल्यूम ड्राई अप
            is_volume_dry = row['Volume'] < row['Avg_Vol']

            # ट्रिगर: Resistance ke paas hona zaroori nahi, bas squeeze ho
            if has_not_broken_out and is_squeezed and is_volume_dry:
                signal_date = df.index[idx].date()
                distance_pct = round(((range_high - row['Close']) / row['Close']) * 100, 2)

                # Distance ke hisaab se Setup Type decide karo
                if distance_pct <= 1.5:
                    setup_type = "READY_TO_BLAST"
                elif distance_pct <= 4.0:
                    setup_type = "SQUEEZE_STAGE_2"
                else:
                    setup_type = "SQUEEZE_STAGE_1"

                signals.append({
                    'Signal_Date': str(signal_date),
                    'Stock': stock.replace('.NS', ''),
                    'Close': round(row['Close'], 2),
                    '14D_Resistance': round(range_high, 2),
                    '14D_Support': round(range_low, 2),
                    'Distance_Pct': distance_pct,
                    'Range_Size_Pct': round(((range_high - range_low) / range_low) * 100, 2),
                    'Recent_Bottom': round(recent_bottom, 2),
                    'Volume': int(row['Volume']),
                    'Avg_Volume': int(row['Avg_Vol']),
                    'Setup_Type': setup_type
                })
    except Exception as e:
        print(f"Logic error on {stock}: {e}", flush=True)
    return signals

# ===== MAIN EXECUTION =====
stocks = get_watchlist_stocks()
all_signals = []
stock_batches = [stocks[i:i + BATCH_SIZE] for i in range(0, len(stocks), BATCH_SIZE)]

print(f"\n=== SCANNING FOR ALL SQUEEZE STAGES ({len(stock_batches)} BATCHES) ===", flush=True)

start_download_dt = START_DATE - timedelta(days=200)

for b_idx, batch in enumerate(stock_batches):
    print(f"Processing Batch [{b_idx+1}/{len(stock_batches)}]...", flush=True)
    try:
        batch_data = yf.download(batch, start=start_download_dt, end=END_DATE + timedelta(days=1), progress=False, group_by='ticker', auto_adjust=False)
        if batch_data.empty: continue

        for stock in batch:
            stock_df = batch_data if len(batch) == 1 else (batch_data[stock] if stock in batch_data.columns.levels[0] else None)
            if stock_df is None: continue

            stock_signals = process_single_stock_data(stock_df, stock, START_DATE, END_DATE)
            if stock_signals:
                all_signals.extend(stock_signals)
                print(f" -> {stock}: Squeeze Setup Found!", flush=True)
        time.sleep(2)
    except Exception as batch_err:
        time.sleep(5)

# ===== UPDATE SHEET =====
ws_output.clear()
if all_signals:
    df_final = pd.DataFrame(all_signals).drop_duplicates(subset=['Signal_Date', 'Stock']).sort_values('Distance_Pct', ascending=True)
    ws_output.update('A1', [df_final.columns.tolist()] + df_final.values.tolist())
    print(f"\n=== FOUND {len(df_final)} SQUEEZE STOCKS ===", flush=True)
    print(f"READY_TO_BLAST: {len(df_final[df_final['Setup_Type']=='READY_TO_BLAST'])}", flush=True)
    print(f"SQUEEZE_STAGE_2: {len(df_final[df_final['Setup_Type']=='SQUEEZE_STAGE_2'])}", flush=True)
    print(f"SQUEEZE_STAGE_1: {len(df_final[df_final['Setup_Type']=='SQUEEZE_STAGE_1'])}", flush=True)
else:
    ws_output.update('A1', [['No Squeeze Found']])
    print(f"\n=== 0 SIGNALS ===", flush=True)
