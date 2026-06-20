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

print("=== REVERSE ENGINEERING V2 - RETEST & CHOCH TRACKER ===", flush=True)

# GCP Sheets Connection
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# STRICT RULES
TARGET_PCT = 10.0        
VALIDATION_SL = 5.0      
MAX_HOLD_DAYS = 30

def get_or_create_ws(sh, title):
    try: 
        ws = sh.worksheet(title)
        ws.clear()
        return ws
    except: 
        return sh.add_worksheet(title=title, rows=50000, cols=12)

def calculate_advanced_features(df):
    # RSI Calculation
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    # Volume Profile
    df['Vol_20MA'] = df['Volume'].rolling(20).mean()
    df['Vol_Multiple'] = df['Volume'] / df['Vol_20MA']
    
    # Structural Support & CHoCH Levels
    df['Support_20D'] = df['Low'].shift(1).rolling(window=20).min()
    df['Recent_High_10D'] = df['High'].shift(1).rolling(window=10).max()
    df['SMA_20'] = df['Close'].rolling(20).mean()
    
    return df

def identify_driving_logic(df, idx):
    """
    सटीक कारण खोजता है कि मूव Retest की वजह से आया या Change of Character (CHoCH) से।
    """
    row = df.iloc[idx]
    row_prev = df.iloc[idx-1] if idx > 0 else row
    reasons = []
    
    # 1. Retest Support Logic (Price testing the structural floor)
    if abs((row['Low'] / row['Support_20D']) - 1) * 100 <= 1.5:
        reasons.append("SUPPORT_RETEST")
        
    # 2. CHoCH / Breakout Logic (Price breaking recent lower highs with momentum)
    if row['Close'] > row['Recent_High_10D'] and row_prev['Close'] <= row_prev['Recent_High_10D']:
        reasons.append("CHoCH_BREAKOUT")
        
    # 3. Volume & RSI Conformation
    if row['Vol_Multiple'] > 2.0:
        reasons.append("INSTITUTIONAL_VOLUME")
    if row['RSI'] > 55 and row_prev['RSI'] <= 55:
        reasons.append("MOMENTUM_SHIFT")
        
    if not reasons:
        reasons.append("PRICE_ACTION_STRUCTURE")
        
    return " & ".join(reasons)

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
        # WHILE LOOP का इस्तेमाल ताकि ट्रेड खत्म होने के बाद ही अगला इंडेक्स चेक हो
        while idx < len(df) - 1:
            current_close = df['Close'].iloc[idx]
            target_price = current_close * (1 + TARGET_PCT / 100)
            sl_price = current_close * (1 - VALIDATION_SL / 100)
            
            # चेक करें कि इस तारीख से आगे बढ़ने पर पहले क्या हिट होता है
            trade_outcome = None
            days_held = 0
            max_gain = 0
            exit_idx = idx + 1
            
            for future_idx in range(idx + 1, min(idx + 1 + MAX_HOLD_DAYS, len(df))):
                f_row = df.iloc[future_idx]
                days_held = future_idx - idx
                
                # मैक्सिमम गेन ट्रैक करें
                current_gain = ((f_row['High'] / current_close) - 1) * 100
                if current_gain > max_gain:
                    max_gain = current_gain
                    
                # Target vs SL Check
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
            
            # अगर इस तारीख से सच में 10% का मूव आया था, तभी लॉग करेंगे
            if max_gain >= TARGET_PCT:
                detected_logic = identify_driving_logic(df, idx)
                setup_date = df.index[idx]
                
                reverse_logs.append({
                    'Stock': stock,
                    'Entry_Date': setup_date,
                    'Exit_Date': df.index[exit_idx],
                    'Max_Gain_Achieved_%': round(max_gain, 2),
                    'Identified_Logic': detected_logic,
                    'Result_Status': trade_outcome,
                    'Days_Held': days_held
                })
                
                # Segregation Tracking
                if detected_logic not in strategy_tracker:
                    strategy_tracker[detected_logic] = {'Total': 0, 'Wins': 0, 'Losses': 0, 'Timeouts': 0}
                strategy_tracker[detected_logic]['Total'] += 1
                if trade_outcome == "PROFIT": strategy_tracker[detected_logic]['Wins'] += 1
                elif trade_outcome == "LOSS": strategy_tracker[detected_logic]['Losses'] += 1
                else: strategy_tracker[detected_logic]['Timeouts'] += 1
                
                # [CRITICAL FIX]: अब लूप सीधे एग्जिट वाले दिन पर जंप कर जाएगा!
                # यानी जब तक पहला मूव पूरा खत्म नहीं होता (प्रॉफिट/लॉस), तब तक बीच की तारीखों (21, 22) पर दोबारा एंट्री काउंट नहीं होगी।
                idx = exit_idx + 1
            else:
                idx += 1

# --- GOOGLE SHEETS UPLOAD ---
print("\nWriting Cleaned Logs to Google Sheets...", flush=True)

ws_logs = get_or_create_ws(sh, "10PCT_REVERSE_LOGS")
if reverse_logs:
    df_rev = pd.DataFrame(reverse_logs).sort_values(by=['Stock', 'Entry_Date'])
    header_rev = df_rev.columns.values.tolist()
    rows_rev = df_rev.values.tolist()
    payload_rev = [header_rev] + rows_rev
    
    for i in range(0, len(payload_rev), 1000):
        ws_logs.append_rows(payload_rev[i:i+1000])
        time.sleep(1)
    print("✔ Cleaned Reverse Logs updated without overlapping entries.")

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
    
    header_sum = ["Strategy Mode (Retest / CHoCH)", "Total Clean Triggers", "Profit Hits", "Loss Hits", "Timeouts", "Pure Win Rate %"]
    ws_summary.update([header_sum] + summary_rows)
    print("✔ Pure Strategy Summary generated.")

print("\n=== ENGINE RUN COMPLETE ===")
