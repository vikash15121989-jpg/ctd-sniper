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

print("=== RS BEATER V36 - 2-DAY LOCK-IN & PERFORMANCE DASHBOARD ===", flush=True)

# GSheet Connection Setup
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

R = {
    'min_daily_value_cr': 30.0,
    'fixed_target_pct': 6.0,       
    'fixed_sl_pct': 3.0,           
    'trail_trigger_pct': 2.5,      
    'time_stop_days': 8,
    'cooldown_days': 4
}

def get_or_create_ws(sh, title):
    try: return sh.worksheet(title)
    except: return sh.add_worksheet(title=title, rows=35000, cols=12)

def calculate_base_indicators(df):
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    df['Low_Min_10D'] = df['Low'].shift(1).rolling(window=10).min()
    df['Support_Zone_20D'] = df['Low'].shift(1).rolling(window=20).min()
    return df

def check_best_combined_pattern(df, idx):
    if idx < 10: return False, "NONE"
    row_today = df.iloc[idx]
    stop_hunt = (row_today['Low'] < row_today['Low_Min_10D']) and (row_today['Close'] > row_today['Low_Min_10D'])
    price_flat_or_down = df['Close'].iloc[idx] <= df['Close'].iloc[idx-10]
    rsi_rising = df['RSI'].iloc[idx] > df['RSI'].iloc[idx-10]
    hidden_accum = price_flat_or_down and rsi_rising
    
    if stop_hunt and hidden_accum: return True, "COMBINED_JACKPOT"
    elif stop_hunt: return True, "STOP_HUNT"
    elif hidden_accum: return True, "HIDDEN_ACCUM"
    return False, "NONE"

def is_bullish_confirmation_candle(row):
    open_p, high_p, low_p, close_p = row['Open'], row['High'], row['Low'], row['Close']
    candle_range = high_p - low_p
    if candle_range <= 0: return False
    
    body_size = abs(close_p - open_p)
    lower_wick = min(open_p, close_p) - low_p
    upper_wick = high_p - max(open_p, close_p)
    is_green = close_p > open_p
    
    is_hammer = (lower_wick >= (body_size * 1.5)) and (upper_wick <= (candle_range * 0.25))
    is_strong_green = is_green and (((close_p / open_p) - 1) * 100 >= 1.0)
    
    return is_hammer or is_strong_green

def download_single_stock(stock):
    try:
        ticker = stock if stock.endswith('.NS') else f"{stock}.NS"
        df = yf.download(ticker, start=BACKTEST_START - timedelta(days=50),
                       end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True, timeout=15)
        if df.empty or len(df) < 30: return None, stock
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        df = calculate_base_indicators(df)
        df.index = pd.to_datetime(df.index).strftime('%Y-%m-%d')
        return df, stock
    except:
        return None, stock

stocks = ws_watchlist.col_values(1)[1:]
stocks = sorted(list(set([s.strip().upper().replace('.NS','') for s in stocks if s.strip()])))
total_stocks = len(stocks)
total_batches = (total_stocks + BATCH_SIZE - 1) // BATCH_SIZE

trade_logs = []

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
        open_trade = None
        last_exit_idx = -100
        
        for idx in range(20, len(df) - 1):
            row = df.iloc[idx]
            current_date = df.index[idx]
            
            if open_trade:
                if row['High'] > open_trade['Max_High']: open_trade['Max_High'] = row['High']
                current_max_profit = ((row['High'] / open_trade['Entry_Price']) - 1) * 100
                current_sl = open_trade['SL_Price']
                if current_max_profit >= R['trail_trigger_pct']: current_sl = open_trade['Entry_Price']

                sl_hit = row['Low'] <= current_sl
                target_hit = row['High'] >= open_trade['Target_Price']
                days_held = idx - open_trade['Entry_Idx']
                
                exit_status = None; exit_price = None
                if sl_hit and target_hit:
                    exit_price = current_sl; exit_status = 'LOSS' if current_sl < open_trade['Entry_Price'] else 'COST_EXIT'
                elif target_hit:
                    exit_price = open_trade['Target_Price']; exit_status = 'PROFIT'
                elif sl_hit:
                    exit_price = current_sl; exit_status = 'LOSS' if current_sl < open_trade['Entry_Price'] else 'COST_EXIT'
                elif days_held >= R['time_stop_days']:
                    exit_price = row['Close']; exit_status = 'TIME_OUT'
                
                if exit_status:
                    pnl_pct = ((exit_price / open_trade['Entry_Price']) - 1) * 100
                    max_runup = ((open_trade['Max_High'] / open_trade['Entry_Price']) - 1) * 100
                    trade_logs.append({
                        'Setup_Date': open_trade['Setup_Date'], 'Exit_Date': current_date, 'Stock': stock,
                        'Pattern_Type': open_trade['Pattern_Type'], 'Entry_Price': round(open_trade['Entry_Price'], 2),
                        'Exit_Price': round(exit_price, 2), 'Max_Runup_%': round(max_runup, 2), 'PnL_%': round(pnl_pct, 2),
                        'Result': exit_status, 'Days_Held': days_held
                    })
                    last_exit_idx = idx; open_trade = None
            else:
                if (idx - last_exit_idx) < R['cooldown_days']: continue
                avg_val = (df['Close'].iloc[max(0,idx-20):idx] * df['Volume'].iloc[max(0,idx-20):idx]).mean() / 1e7
                if pd.isna(avg_val) or avg_val < R['min_daily_value_cr']: continue
                
                is_pattern, pattern_type = check_best_combined_pattern(df, idx)
                if is_pattern:
                    if is_bullish_confirmation_candle(row):
                        support_line = row['Support_Zone_20D']
                        pct_from_support = ((row['Low'] / support_line) - 1) * 100
                        
                        if pct_from_support <= 1.5:
                            if idx + 1 < len(df):
                                next_row = df.iloc[idx + 1]
                                if next_row['Close'] > row['High']:
                                    open_trade = {
                                        'Setup_Date': df.index[idx + 1], 
                                        'Entry_Price': next_row['Close'],
                                        'Target_Price': next_row['Close'] * (1 + R['fixed_target_pct']/100),
                                        'SL_Price': next_row['Close'] * (1 - R['fixed_sl_pct']/100),
                                        'Entry_Idx': idx + 1,
                                        'Pattern_Type': f"{pattern_type}_CONFIRMED",
                                        'Max_High': next_row['High']
                                    }

# --- ADVANCED DASHBOARD GENERATION & UPLOAD ENGINE ---
print("\nConnecting to Google Sheet...", flush=True)
ws_datewise = get_or_create_ws(sh, "PA_DATEWISE_LOGS")
ws_datewise.clear()
time.sleep(2)

if trade_logs:
    df_logs = pd.DataFrame(trade_logs).sort_values(by='Setup_Date', ascending=True)
    
    # --- CALCULATE METRICS ---
    total_trades = len(df_logs)
    wins = len(df_logs[df_logs['Result'] == 'PROFIT'])
    losses = len(df_logs[df_logs['Result'] == 'LOSS'])
    cost_exits = len(df_logs[df_logs['Result'] == 'COST_EXIT'])
    timeouts = len(df_logs[df_logs['Result'] == 'TIME_OUT'])
    
    win_rate = round((wins / total_trades) * 100, 2) if total_trades > 0 else 0.0
    net_pnl = round(df_logs['PnL_%'].sum(), 2)
    avg_pnl_per_trade = round(df_logs['PnL_%'].mean(), 2)

    # --- BUILD DASHBOARD FORMAT ---
    dashboard = [
        ["📊 STRATEGY PERFORMANCE DASHBOARD", "", "", "", "", "", "", "", "", ""],
        ["Total Trades", "Wins (Targets)", "Losses (SL)", "Cost Exits", "Time Outs", "WIN RATE %", "NET P&L %", "AVG P&L/TRADE", "", ""],
        [total_trades, wins, losses, cost_exits, timeouts, f"{win_rate}%", f"{net_pnl}%", f"{avg_pnl_per_trade}%", "", ""],
        ["", "", "", "", "", "", "", "", "", ""],  # Empty Spacer Row
    ]
    
    # Append the main logs data headers and values below row 4
    header = df_logs.columns.values.tolist()
    all_rows = df_logs.values.tolist()
    
    # Combined payload: Dashboard (Rows 1-4) + Main Data (Row 5 onwards)
    payload = dashboard + [header] + all_rows
    
    chunk_size = 1000
    print(f"Uploading {len(payload)} rows including Dashboard Summary...", flush=True)
    for i in range(0, len(payload), chunk_size):
        chunk = payload[i:i + chunk_size]
        if i == 0: ws_datewise.update(chunk)
        else: ws_datewise.append_rows(chunk)
        time.sleep(1.5)
    print(f"\n[VERIFIED] Dashboard & Datewise Logs successfully saved!", flush=True)
else:
    print("\n[INFO] No trades matched.", flush=True)
    ws_datewise.update([["System_Status"], ["No Trades Matched the 2-Day High Breakout filter."]])
        
