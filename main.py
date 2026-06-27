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

print("=== V33.0: 100% PURE PRICE ACTION STRENGTH SCANNER ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== CONFIG =====
END_DATE = datetime(2026, 6, 25).date() 
START_DATE = datetime(2025, 6, 25).date() 
BATCH_SIZE = 15  # एक बार में 15 स्टॉक्स डाउनलोड होंगे

R = {
    'backtest_start': START_DATE,
    'backtest_end': END_DATE,
    'lookback_days': 14,
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

def check_structure_and_choch(df, idx, lookback=60):
    """बिना इंडिकेटर के स्विंग स्ट्रक्चर चेक करना"""
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
        if len(df) < 80: return [] # पर्याप्त डेटा होना जरूरी है

        # Base 20 EMA केवल ट्रेंड दिशा जानने के लिए (स्ट्रेंथ के लिए नहीं)
        df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
        df['20_std'] = df['Close'].rolling(window=20).std()
        df['Squeeze_Threshold'] = df['20_std'].rolling(window=50).quantile(0.20)
        df['Avg_Vol'] = df['Volume'].rolling(window=20).mean()

        df_scan = df[(df.index >= pd.to_datetime(start_date)) & (df.index <= pd.to_datetime(end_date))]

        for i in range(len(df_scan)):
            idx = df.index.get_loc(df_scan.index[i])
            if idx < 20: continue
            
            row = df.iloc[idx]
            prev_row = df.iloc[idx-1]
            prev_2_row = df.iloc[idx-2]

            # FILTER 1: बुनियादी दिशा फ़िल्टर
            if row['Close'] < row['EMA20']: continue

            # FILTER 2: चेंज ऑफ कैरेक्टर / स्ट्रक्चर फ़िल्टर
            struct_valid, recent_bottom = check_structure_and_choch(df, idx)
            if not struct_valid: continue

            # =======================================================
            # 💎 PURE PRICE ACTION STRENGTH ENGINE (NO INDICATORS)
            # =======================================================
            
            # 1. CANDLE BODY RATIO: कैंडल की क्लोजिंग मजबूत होनी चाहिए (नो बड़ी ऊपरी पूंछ/wick)
            candle_range = row['High'] - row['Low']
            body_size = abs(row['Close'] - row['Open'])
            is_strong_candle = body_size >= (candle_range * 0.70) if candle_range > 0 else False

            # 2. SLOPE ACCELERATION: पिछले 5 दिनों की तेजी, उसके पिछले 15 दिनों की औसत चाल से दोगुनी होनी चाहिए
            back_5_change = abs(row['Close'] - df.iloc[idx-5]['Close'])
            back_15_change = abs(df.iloc[idx-5]['Close'] - df.iloc[idx-20]['Close'])
            is_fast_slope = back_5_change > (back_15_change * 2)

            # 3. VOLUME ESCALATION: आज का वॉल्यूम पिछले 2 दिनों के वॉल्यूम से लगातार बढ़ रहा हो
            is_volume_rising = row['Volume'] > prev_row['Volume'] and prev_row['Volume'] > prev_2_row['Volume']

            # अगर इनमें से कोई भी प्राइस एक्शन स्ट्रेंथ मैच नहीं होती, तो स्टॉक रिजेक्ट
            if not (is_strong_candle and is_fast_slope and is_volume_rising): 
                continue

            # =======================================================
            # FILTER 4: BREAKOUT OR SQUEEZE LOGIC
            # =======================================================
            recent_14 = df.iloc[idx-R['lookback_days']:idx]
            range_high = recent_14['High'].max()
            
            is_breakout = row['Close'] > range_high
            is_high_volume = row['Volume'] > (row['Avg_Vol'] * 1.5)
            is_squeezed = row['20_std'] <= row['Squeeze_Threshold']

            if not (is_breakout and is_high_volume):
                if not (is_squeezed and row['Close'] > row['EMA20']):
                    continue

            signal_date = df.index[idx].date()
            signals.append({
                'Signal_Date': str(signal_date),
                'Stock': stock.replace('.NS', ''),
                'Close': round(row['Close'], 2),
                'EMA20': round(row['EMA20'], 2),
                'Recent_Bottom': round(recent_bottom, 2),
                'Volume': int(row['Volume']),
                'Avg_Volume': int(row['Avg_Vol']),
                'Setup_Type': "PA_STRONG_BREAKOUT" if is_breakout else "PA_STRONG_SQUEEZE"
            })
    except Exception as e:
        print(f"Logic error on {stock}: {e}", flush=True)
    return signals

# ===== MAIN EXECUTION WITH BATCHING =====
stocks = get_watchlist_stocks()
all_signals = []

stock_batches = [stocks[i:i + BATCH_SIZE] for i in range(0, len(stocks), BATCH_SIZE)]
print(f"\n=== STARTING PURE PRICE ACTION SCAN: {len(stock_batches)} Batches ===", flush=True)

start_download_dt = START_DATE - timedelta(days=200)

for b_idx, batch in enumerate(stock_batches):
    print(f"\nProcessing Batch [{b_idx+1}/{len(stock_batches)}]: {len(batch)} stocks...", flush=True)
    try:
        batch_data = yf.download(batch, start=start_download_dt, end=END_DATE + timedelta(days=1), progress=False, group_by='ticker', auto_adjust=False)
        
        if batch_data.empty: continue

        for stock in batch:
            if len(batch) == 1:
                stock_df = batch_data
            else:
                if stock in batch_data.columns.levels[0]:
                    stock_df = batch_data[stock]
                else:
                    continue

            stock_signals = process_single_stock_data(stock_df, stock, START_DATE, END_DATE)
            if stock_signals:
                all_signals.extend(stock_signals)
                print(f" -> {stock}: Pure Price Action Strength Found!", flush=True)

        time.sleep(3)

    except Exception as batch_err:
        print(f"Error processing Batch {b_idx+1}: {batch_err}", flush=True)
        time.sleep(5)

# ===== GOOGLE SHEET UPDATE =====
ws_output.clear()
if all_signals:
    df_final = pd.DataFrame(all_signals).drop_duplicates(subset=['Signal_Date', 'Stock']).sort_values('Signal_Date', ascending=False)
    ws_output.update('A1', [df_final.columns.tolist()] + df_final.values.tolist())
    print(f"\n=== SCAN COMPLETED | FOUND {len(df_final)} PURE ACTION SIGNALS ===", flush=True)
    print(df_final.head())
else:
    ws_output.update('A1', [['No Pure Strength Signals Found']])
    print(f"\n=== SCAN COMPLETED | 0 SIGNALS ===", flush=True)
    
