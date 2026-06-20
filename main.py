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

print("=== PURE PRICE ACTION RAW BACKTEST ENGINE ===", flush=True)

# GCP Sheets Connection
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# PRICE ACTION BACKTEST PARAMETERS
TARGET_PCT = 10.0        
VALIDATION_SL = 5.0      
MAX_HOLD_DAYS = 30
COOLDOWN_DAYS = 8        # एक ट्रेड के बाद स्टॉक को सेट होने का समय दें

def get_or_create_ws(sh, title):
    try: 
        ws = sh.worksheet(title)
        ws.clear()
        return ws
    except: 
        return sh.add_worksheet(title=title, rows=50000, cols=12)

def calculate_price_action_features(df):
    # 1. Support Zone: पिछले 20 दिनों का सबसे निचला स्तर (Floor)
    df['Support_20D'] = df['Low'].shift(1).rolling(window=20).min()
    
    # 2. Resistance Line (CHoCH के लिए): पिछले 10 दिनों का उच्चतम स्तर
    df['Resistance_10D'] = df['High'].shift(1).rolling(window=10).max()
    
    # 3. Volume Breakdown (Institutional Activity)
    df['Vol_20MA'] = df['Volume'].rolling(20).mean()
    df['Vol_Multiple'] = df['Volume'] / df['Vol_20MA']
    
    return df

def check_pure_price_action(df, idx):
    """
    बिना किसी लैगिंग इंडिकेटर के, सिर्फ कैंडल और स्ट्रक्चर को देखकर 
    एंट्री सिग्नल जनरेट करता है।
    """
    row = df.iloc[idx]
    row_prev = df.iloc[idx-1] if idx > 0 else row
    
    # कैंडल की बनावट (Body & Wicks)
    open_p, high_p, low_p, close_p = row['Open'], row['High'], row['Low'], row['Close']
    candle_range = high_p - low_p
    if candle_range <= 0: return False, "NONE"
    
    body_size = abs(close_p - open_p)
    lower_wick = min(open_p, close_p) - low_p
    is_green = close_p > open_p
    
    # --- PATTERN 1: SUPPORT RETEST + BUYER REJECTION ---
    # प्राइस पुराने सपोर्ट के पास (1.2% के दायरे में) आया और नीचे से रिजेक्शन (लंबी पूँछ) दिखाई
    at_support = abs((row['Low'] / row['Support_20D']) - 1) * 100 <= 1.2
    has_buyer_rejection = lower_wick >= (body_size * 1.2) # कैंडल के नीचे लंबी पूँछ है
    
    if at_support and (has_buyer_rejection or is_green):
        return True, "PA_SUPPORT_RETEST"
        
    # --- PATTERN 2: CHoCH BREAKOUT (VOLUME BACKED) ---
    # प्राइस ने पिछले 10 दिनों के हाई (रेसिस्टेंस) को ब्रेक किया और मजबूत ग्रीन कैंडल बनाई
    broke_resistance = row['Close'] > row['Resistance_10D'] and row_prev['Close'] <= row_prev['Resistance_10D']
    strong_volume = row['Vol_Multiple'] > 1.8 # सामान्य से दोगुना वॉल्यूम (बड़े प्लेयर्स की एंट्री)
    
    if broke_resistance and strong_volume and is_green:
        return True, "PA_CHoCH_BREAKOUT"
        
    return False, "NONE"

def download_single_stock(stock):
    try:
        ticker = stock if stock.endswith('.NS') else f"{stock}.NS"
        df = yf.download(ticker, start=BACKTEST_START - timedelta(days=60),
                       end=BACKTEST_END + timedelta(days=5), progress=False, auto_adjust=True, timeout=15)
        if df.empty or len(df) < 40: return None, stock
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        df = calculate_price_action_features(df)
        df.index = pd.to_datetime(df.index).strftime('%Y-%m-%d')
        return df, stock
    except:
        return None, stock

# --- MAIN SYSTEM EXECUTION ---
stocks = ws_watchlist.col_values(1)[1:]
stocks = sorted(list(set([s.strip().upper().replace('.NS','') for s in stocks if s.strip()])))
total_stocks = len(stocks)
total_batches = (total_stocks + BATCH_SIZE - 1) // BATCH_SIZE

pa_logs = []
strategy_tracker = {}

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
        idx = 20
        while idx < len(df) - 1:
            # स्टेप 1: आज की तारीख पर प्योर प्राइस एक्शन चेक करो
            has_setup, pa_logic = check_pure_price_action(df, idx)
            
            if has_setup:
                entry_price = df['Close'].iloc[idx]
                target_price = entry_price * (1 + TARGET_PCT / 100)
                sl_price = entry_price * (1 - VALIDATION_SL / 100)
                
                trade_outcome = None
                exit_idx = idx + 1
                max_gain = 0
                
                # स्टेप 2: एंट्री होने के बाद अगले 30 दिनों का लाइव सफर ट्रैक करो
                for future_idx in range(idx + 1, min(idx + 1 + MAX_HOLD_DAYS, len(df))):
                    f_row = df.iloc[future_idx]
                    
                    current_gain = ((f_row['High'] / entry_price) - 1) * 100
                    if current_gain > max_gain:
                        max_gain = current_gain
                        
                    if f_row['High'] >= target_price:
                        trade_outcome = "PROFIT"
                        exit_idx = future_idx
                        break
                    elif f_row['Low'] <= sl_price:
                        trade_outcome = "LOSS"
                        exit_idx = future_idx
                        break
                
                if not trade_outcome:
                    trade_outcome = "TIMEOUT"
                    exit_idx = min(idx + MAX_HOLD_DAYS, len(df) - 1)
                
                # लॉग में डेटा डालें
                pa_logs.append({
                    'Stock': stock,
                    'Entry_Date': df.index[idx],
                    'Exit_Date': df.index[exit_idx],
                    'Entry_Price': round(entry_price, 2),
                    'Max_Gain_%': round(max_gain, 2),
                    'PA_Pattern': pa_logic,
                    'Outcome': trade_outcome,
                    'Days_Held': exit_idx - idx
                })
                
                # सेग्रिगेशन (Metrics Tracking)
                if pa_logic not in strategy_tracker:
                    strategy_tracker[pa_logic] = {'Total': 0, 'Wins': 0, 'Losses': 0, 'Timeouts': 0}
                strategy_tracker[pa_logic]['Total'] += 1
                if trade_outcome == "PROFIT": strategy_tracker[pa_logic]['Wins'] += 1
                elif trade_outcome == "LOSS": strategy_tracker[pa_logic]['Losses'] += 1
                else: strategy_tracker[pa_logic]['Timeouts'] += 1
                
                # ट्रेड खत्म होने के बाद स्टॉक को आराम दें (Cooldown)
                idx = exit_idx + COOLDOWN_DAYS
            else:
                idx += 1

# --- GOOGLE SHEETS GRAPH WRITE ---
print("\nUploading Pure Price Action Performance to Google Sheets...", flush=True)

# Sheet 1: Detailed Logs
ws_logs = get_or_create_ws(sh, "10PCT_REVERSE_LOGS")
if pa_logs:
    df_rev = pd.DataFrame(pa_logs).sort_values(by=['Stock', 'Entry_Date'])
    header_rev = df_rev.columns.values.tolist()
    payload_rev = [header_rev] + df_rev.values.tolist()
    
    for i in range(0, len(payload_rev), 1000):
        ws_logs.append_rows(payload_rev[i:i+1000])
        time.sleep(1)

# Sheet 2: Pure Performance Summary
ws_summary = get_or_create_ws(sh, "STRATEGY_PERFORMANCE_SUMMARY")
if strategy_tracker:
    summary_rows = []
    for logic, metrics in strategy_tracker.items():
        total = metrics['Total']
        wins = metrics['Wins']
        losses = metrics['Losses']
        timeouts = metrics['Timeouts']
        win_rate = round((wins / total) * 100, 2) if total > 0 else 0.0
        summary_rows.append([logic, total, wins, losses, timeouts, f"{win_rate}%"])
    
    header_sum = ["Price Action Mode", "Total Signal Count", "Target Hits (10% Profit)", "StopLoss Hits (5% Loss)", "Timeouts", "Real Price Action Win Rate %"]
    ws_summary.update([header_sum] + summary_rows)

print("\n=== PURE PRICE ACTION TEST DONE ===")
