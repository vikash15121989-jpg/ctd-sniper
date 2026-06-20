import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import warnings
warnings.filterwarnings('ignore')

BACKTEST_END = datetime.now().date()
BACKTEST_START = BACKTEST_END - timedelta(days=365)
BATCH_SIZE = 35

print("=== REVERSE ENGINEERING & PATTERN DISCOVERY ENGINE ===", flush=True)

# GCP Sheets Connection
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# REVERSE ENGINEERING PARAMETERS
LOOK_AHEAD_DAYS = 15     # 10% प्रॉफिट कितने दिनों के अंदर आना चाहिए
TARGET_PCT = 10.0        # खोजा जाने वाला मिनिमम प्रॉफिट प्रतिशत
VALIDATION_SL = 5.0      # स्ट्रेटजी टेस्ट करते वक्त स्टॉपलॉस प्रतिशत

def get_or_create_ws(sh, title):
    try: 
        ws = sh.worksheet(title)
        ws.clear()
        return ws
    except: 
        return sh.add_worksheet(title=title, rows=50000, cols=12)

def calculate_advanced_features(df):
    # RSI
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    # Volume Analytics
    df['Vol_20MA'] = df['Volume'].rolling(20).mean()
    df['Vol_Multiple'] = df['Volume'] / df['Vol_20MA']
    
    # Price Action & Support
    df['Low_Min_10D'] = df['Low'].shift(1).rolling(window=10).min()
    df['SMA_20'] = df['Close'].rolling(20).mean()
    df['SMA_50'] = df['Close'].rolling(50).mean()
    
    return df

def identify_driving_logic(df, idx):
    """
    यह फंक्शन 10% मूव आने के दिन के बैकग्राउंड डेटा को देखकर 
    उसका मुख्य कारण (Logic) डिकोड करता है।
    """
    row = df.iloc[idx]
    row_prev = df.iloc[idx-1] if idx > 0 else row
    
    reasons = []
    
    # 1. Volume Spurt Logic
    if row['Vol_Multiple'] > 2.5:
        reasons.append("HIGH_VOLUME_BREAKOUT")
    
    # 2. RSI Oversold Recovery / Divergence Logic
    if row['RSI'] < 35:
        reasons.append("OVERSOLD_REBOUND")
    elif row['RSI'] > 55 and row_prev['RSI'] <= 55:
        reasons.append("RSI_MOMENTUM_SHIFT")
        
    # 3. Support & Stop Hunt Logic
    if row['Low'] < row['Low_Min_10D'] and row['Close'] > row['Low_Min_10D']:
        reasons.append("STOP_HUNT_SHAKEOUT")
        
    # 4. Moving Average Support
    if row['Low'] <= row['SMA_20'] and row['Close'] > row['SMA_20']:
        reasons.append("20SMA_DYNAMIC_SUPPORT")
        
    # Default Logic if nothing specific matches
    if not reasons:
        if row['Close'] > row['Open']:
            reasons.append("STRONG_BULLISH_CANDLE")
        else:
            reasons.append("VOLATILITY_EXPANSION")
            
    return " & ".join(reasons)

def validate_strategy_performance(df, start_idx, target_pct, sl_pct):
    """
    जब वो लॉजिक दोबारा बना, तो कितनी बार प्रॉफिट और कितनी बार लॉस हुआ, 
    यह उसे टेस्ट करता है।
    """
    entry_price = df['Close'].iloc[start_idx]
    target_price = entry_price * (1 + target_pct / 100)
    sl_price = entry_price * (1 - sl_pct / 100)
    
    for i in range(start_idx + 1, min(start_idx + 30, len(df))): # Max 30 days validation holding
        row = df.iloc[i]
        if row['High'] >= target_price:
            return "PROFIT", i - start_idx
        if row['Low'] <= sl_price:
            return "LOSS", i - start_idx
            
    return "TIMEOUT/FAIL", 30

def download_single_stock(stock):
    try:
        ticker = stock if stock.endswith('.NS') else f"{stock}.NS"
        df = yf.download(ticker, start=BACKTEST_START - timedelta(days=60),
                       end=BACKTEST_END + timedelta(days=5), progress=False, auto_adjust=True, timeout=15)
        if df.empty or len(df) < 40: return None, stock
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        df = calculate_advanced_features(df)
        df.index = pd.to_datetime(df.index).strftime('%Y-%m-%d')
        return df, stock
    except:
        return None, stock

# --- MAIN ENGINE EXECUTION ---
stocks = ws_watchlist.col_values(1)[1:]
stocks = sorted(list(set([s.strip().upper().replace('.NS','') for s in stocks if s.strip()])))
total_stocks = len(stocks)
total_batches = (total_stocks + BATCH_SIZE - 1) // BATCH_SIZE

reverse_logs = []
strategy_tracker = {} # अलग-अलग लॉजिक्स की एक्यूरेसी ट्रैक करने के लिए

for batch_num in range(total_batches):
    start_idx = batch_num * BATCH_SIZE
    end_idx = min(start_idx + BATCH_SIZE, total_stocks)
    batch_stocks = stocks[start_idx:end_idx]

    stock_data = {}
    with ThreadPoolExecutor(max_workers=12) as executor:
        future_to_stock = {executor.submit(download_single_stock, stock): stock for stock in batch_stocks}
        for future in as_completed(future_to_stock):
            df, stock = future.result()
            if df is not None: stock_data[stock] = df

    for stock, df in stock_data.items():
        # हम अंतिम LOOK_AHEAD_DAYS को छोड़ देंगे क्योंकि वहाँ से 10% चेक करने का पूरा समय नहीं मिलेगा
        for idx in range(20, len(df) - LOOK_AHEAD_DAYS):
            current_close = df['Close'].iloc[idx]
            
            # आगे आने वाले दिनों का मैक्सिमम हाई खोजें
            future_window = df['High'].iloc[idx + 1 : idx + 1 + LOOK_AHEAD_DAYS]
            max_future_high = future_window.max()
            
            # चेक करें कि क्या 10% से ज्यादा का प्रॉफिट हुआ
            potential_gain = ((max_future_high / current_close) - 1) * 100
            
            if potential_gain >= TARGET_PCT:
                # 10% प्रॉफिट मिला! अब इसका कारण खोजते हैं
                detected_logic = identify_driving_logic(df, idx)
                setup_date = df.index[idx]
                
                # अब इस सटीक लॉजिक को टेस्ट करते हैं कि पास्ट में इसके कारण प्रॉफिट हुआ या लॉस
                perf_result, days_taken = validate_strategy_performance(df, idx, TARGET_PCT, VALIDATION_SL)
                
                # लॉग में सेव करें
                reverse_logs.append({
                    'Stock': stock,
                    'Date_Of_Origin': setup_date,
                    'Max_Gain_Achieved_%': round(potential_gain, 2),
                    'Identified_Logic': detected_logic,
                    'Strategy_Test_Result': perf_result,
                    'Days_To_Result': days_taken
                })
                
                # स्ट्रेटजी ट्रैकर को अपडेट करें (Segregation)
                if detected_logic not in strategy_tracker:
                    strategy_tracker[detected_logic] = {'Total_Triggers': 0, 'Profits': 0, 'Losses': 0, 'Timeouts': 0}
                
                strategy_tracker[detected_logic]['Total_Triggers'] += 1
                if perf_result == "PROFIT": strategy_tracker[detected_logic]['Profits'] += 1
                elif perf_result == "LOSS": strategy_tracker[detected_logic]['Losses'] += 1
                else: strategy_tracker[detected_logic]['Timeouts'] += 1
                
                # एक बार मूव मिल जाने पर कुल्डॉउन दें ताकि एक ही रैली को बार-बार रिकॉर्ड न करे
                idx += LOOK_AHEAD_DAYS 

# --- GOOGLE SHEETS UPLOAD ---
print("\nProcessing and Writing Logs to Google Sheets...", flush=True)

# Sheet 1: Stock-wise Detailed Reverse Logs
ws_logs = get_or_create_ws(sh, "10PCT_REVERSE_LOGS")
if reverse_logs:
    df_rev = pd.DataFrame(reverse_logs).sort_values(by=['Stock', 'Date_Of_Origin'])
    header_rev = df_rev.columns.values.tolist()
    rows_rev = df_rev.values.tolist()
    payload_rev = [header_rev] + rows_rev
    
    for i in range(0, len(payload_rev), 1000):
        ws_logs.append_rows(payload_rev[i:i+1000])
        time.sleep(1)
    print("✔ Detailed Reverse Logs updated.")
else:
    ws_logs.update([["Status"], ["No 10% moves found matching structural data."]])

# Sheet 2: Strategy Segregation & Modes Test Summary
ws_summary = get_or_create_ws(sh, "STRATEGY_PERFORMANCE_SUMMARY")
if strategy_tracker:
    summary_rows = []
    for logic, metrics in strategy_tracker.items():
        total = metrics['Total_Triggers']
        wins = metrics['Profits']
        losses = metrics['Losses']
        timeouts = metrics['Timeouts']
        win_rate = round((wins / total) * 100, 2) if total > 0 else 0.0
        
        summary_rows.append([
            logic, total, wins, losses, timeouts, f"{win_rate}%"
        ])
    
    header_sum = ["Identified Core Logic / Strategy Mode", "Total Times Triggered", "Profit Hits (Target 10%)", "Loss Hits (SL 5%)", "Timeouts/Flat", "Win Rate %"]
    payload_sum = [header_sum] + summary_rows
    ws_summary.update(payload_sum)
    print("✔ Strategy Segregation & Modes Summary updated successfully.")
else:
    ws_summary.update([["Status"], ["No Strategy modes could be computed."]])

print("\n=== SYSTEM EXECUTION COMPLETE ===")
