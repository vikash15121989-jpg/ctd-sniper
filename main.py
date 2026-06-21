import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
import time
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

print("=== MOMENTUM BREAKOUT ENGINE V8.5 ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== 1. SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

R = {
    'min_price': 50,
    'cooldown_days': 15
}

def get_or_create_ws(sh, title):
    try: return sh.worksheet(title)
    except: return sh.add_worksheet(title=title, rows=1000, cols=15)

ws_live = get_or_create_ws(sh, "LIVE_TRADES_V8_3")

# ===== 2. MOMENTUM & TRAILING INDICATORS =====
def build_indicators(df):
    if len(df) < 50: return df
    
    # Entry Rule: 20 Day High Breakout
    df['Breakout_High_20D'] = df['High'].shift(1).rolling(window=20).max()
    
    # Trailing Exit Rule: 10 Day Low (Exits when trend bends)
    df['Exit_Low_10D'] = df['Low'].shift(1).rolling(window=10).min()
    
    # Trend Filter
    df['EMA_50'] = df['Close'].ewm(span=50, adjust=False).mean()
    
    # Volume Filter
    df['Vol_20MA'] = df['Volume'].shift(1).rolling(window=20).mean()
    df['Vol_Multiple'] = df['Volume'] / (df['Vol_20MA'] + 1e-5)
    
    return df

def check_momentum_signal(df, idx):
    if idx < 1: return False
    row = df.iloc[idx]
    row_prev = df.iloc[idx-1]
    
    # Condition 1: Stock 50 EMA ke upar ho (Bullish Structure)
    if row['Close'] < row['EMA_50']: return False
    
    # Condition 2: 20-Day High Ka Fresh Breakout with Good Volume
    fresh_breakout = row['Close'] > row['Breakout_High_20D'] and row_prev['Close'] <= row_prev['Breakout_High_20D']
    good_volume = row['Vol_Multiple'] > 1.5
    
    if fresh_breakout and good_volume and (row['Close'] > row['Open']):
        return True
        
    return False

# ===== 3. TICKER CLEANING =====
raw_stocks = ws_watchlist.col_values(1)[1:]
stocks = []
for s in raw_stocks:
    cleaned = s.strip().upper().replace("$", "")
    if cleaned and cleaned not in ['SYMBOL', 'TICKER', 'STOCKS', 'STOCK']:
        stocks.append(cleaned)
stocks = sorted(list(set(stocks)))

print(f"\nScanning {len(stocks)} stocks for Big Momentum Trends...", flush=True)

all_historical_signals = []

# ===== 4. TREND FOLLOWING SIMULATION =====
for stock in stocks:
    try:
        ticker_formatted = f"{stock}.NS"
        df = yf.download(ticker_formatted, period="1y", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        if df.empty or len(df) < 50: continue
        
        df = build_indicators(df)
        total_rows = len(df)
        idx = 21
        
        while idx < total_rows:
            row = df.iloc[idx]
            if row['Close'] < R['min_price']:
                idx += 1
                continue
                
            if check_momentum_signal(df, idx):
                entry_price = row['Close']
                entry_date = df.index[idx]
                
                # Initial Stop Loss at 10 Day Low
                initial_sl = row['Exit_Low_10D']
                if initial_sl >= entry_price or ((entry_price - initial_sl)/entry_price) > 0.12:
                    # Agar SL 12% se zyada bada hai, toh skip risk management ke liye
                    initial_sl = entry_price * 0.93 
                
                exit_date = None
                exit_price = entry_price
                result = "LOSS"
                
                # Dynamic Trailing Loop (No Max Hold Days constraint!)
                exit_idx = idx + 1
                while exit_idx < total_rows:
                    f_row = df.iloc[exit_idx]
                    current_trailing_sl = f_row['Exit_Low_10D']
                    
                    # Agar price trailing SL ke niche jati hai -> EXIT
                    if f_row['Low'] <= current_trailing_sl:
                        exit_price = current_trailing_sl
                        exit_date = df.index[exit_idx]
                        if exit_price > entry_price:
                            result = "WIN"
                        break
                        
                    exit_price = f_row['Close']
                    exit_date = df.index[exit_idx]
                    exit_idx += 1
                
                pnl_pct = round((exit_price / entry_price - 1) * 100, 1)
                
                all_historical_signals.append({
                    'Stock': stock,
                    'Entry_Date': entry_date.strftime('%Y-%m-%d'),
                    'Entry': round(entry_price, 2),
                    'SL_Triggered': round(exit_price, 2),
                    'Status': result,
                    'Exit_Date': exit_date.strftime('%Y-%m-%d') if exit_date else "N/A",
                    'PnL_%': pnl_pct,
                    'Strategy_Mode': "MOMENTUM_BREAKOUT"
                })
                idx = exit_idx + R['cooldown_days']
            else:
                idx += 1
                
        time.sleep(0.03)
    except Exception:
        continue

# ===== 5. SHEET UPDATES =====
if not all_historical_signals:
    print("\n⚠️ No Momentum signals generated with this setup.", flush=True)
else:
    df_results = pd.DataFrame(all_historical_signals)
    
    total_trades = len(df_results)
    wins = len(df_results[df_results['Status'] == 'WIN'])
    losses = len(df_results[df_results['Status'] == 'LOSS'])
    winrate = round((wins / total_trades) * 100, 1) if total_trades else 0
    
    print("\n=======================================================")
    print("🚀 NEW MOMENTUM ENGINE PERFORMANCE REPORT 🚀")
    print("=======================================================")
    print(f"Total System Signals    : {total_trades}")
    print(f"Profitable Trends (Wins): {wins}")
    print(f"Calculated Losses       : {losses}")
    print(f"New Strategic Win Rate  : {winrate}%")
    print("=======================================================\n")
    
    try:
        ws_live.clear()
        df_sheet = df_results.fillna("")
        ws_live.update([df_sheet.columns.values.tolist()] + df_sheet.values.tolist())
        
        ws_summary = get_or_create_ws(sh, "LIVE_SUMMARY")
        ws_summary.clear()
        summary_df = pd.DataFrame([{
            'Execution_Date': datetime.now().strftime('%Y-%m-%d'),
            'Total_Trades': total_trades,
            'Winrate_%': winrate,
            'Wins': wins,
            'Losses': losses
        }])
        ws_summary.update([summary_df.columns.values.tolist()] + summary_df.values.tolist())
        print("=== GSHEET UPDATED WITH MOMENTUM METRICS ===", flush=True)
    except Exception as e:
        print(f"❌ Sheet update failed: {str(e)}", flush=True)
        
