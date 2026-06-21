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

print("=== OPTIMIZED PRICE ACTION ENGINE V8.4 ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== 1. SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# Optimized Backtest Parameters
R = {
    'min_price': 50,
    'max_hold_days': 30,
    'cooldown_days': 10,
    'target_r_multiple': 2.0, # 1:2 Risk-Reward based on ATR/Structure
}

def get_or_create_ws(sh, title):
    try: return sh.worksheet(title)
    except: return sh.add_worksheet(title=title, rows=1000, cols=15)

ws_live = get_or_create_ws(sh, "LIVE_TRADES_V8_3")

# ===== 2. IMPROVED INDICATORS (200 EMA & ATR ADDED) =====
def build_indicators(df):
    if len(df) < 200: return df
    
    # Trend Filter
    df['EMA_200'] = df['Close'].ewm(span=200, adjust=False).mean()
    
    # Price Action Structure
    df['Support_20D'] = df['Low'].shift(1).rolling(window=20).min()
    df['Resistance_10D'] = df['High'].shift(1).rolling(window=10).max()
    df['Vol_20MA'] = df['Volume'].shift(1).rolling(window=20).mean()
    df['Vol_Multiple'] = df['Volume'] / (df['Vol_20MA'] + 1e-5)
    
    # ATR Calculation for Dynamic SL
    high_low = df['High'] - df['Low']
    high_cp = abs(df['High'] - df['Close'].shift(1))
    low_cp = abs(df['Low'] - df['Close'].shift(1))
    tr = pd.concat([high_low, high_cp, low_cp], axis=1).max(axis=1)
    df['ATR_14'] = tr.rolling(window=14).mean()
    
    return df

def check_price_action_at_index(df, idx):
    if idx < 1: return False, None
    
    row = df.iloc[idx]
    row_prev = df.iloc[idx-1]
    
    # FILTER 1: Girte huye market/stock se bachne ke liye 200 EMA Check
    if row['Close'] < row['EMA_200']: return False, None
    
    is_green = row['Close'] > row['Open']
    
    # Strategy 1: Support Retest
    low_near_support = ((row['Low'] / row['Support_20D']) - 1) * 100 <= 1.5
    body = abs(row['Close'] - row['Open'])
    lower_wick = min(row['Open'], row['Close']) - row['Low']
    strong_rejection = lower_wick >= (body * 1.2)
    
    if low_near_support and (strong_rejection or is_green):
        return True, "PA_SUPPORT_RETEST"
        
    # Strategy 2: CHoCH Breakout (SUDHAR: Volume condition 1.25x se badhakar 2.0x ki gayi)
    broke_resistance = row['Close'] > row['Resistance_10D'] and row_prev['Close'] <= row_prev['Resistance_10D']
    strong_volume = row['Vol_Multiple'] > 2.0
    
    if broke_resistance and strong_volume and is_green:
        return True, "PA_CHoCH_BREAKOUT"
        
    return False, None

# ===== 3. READ & CLEAN TICKERS =====
raw_stocks = ws_watchlist.col_values(1)[1:]
stocks = []
for s in raw_stocks:
    cleaned = s.strip().upper().replace("$", "")
    if cleaned and cleaned not in ['SYMBOL', 'TICKER', 'STOCKS', 'STOCK']:
        stocks.append(cleaned)
stocks = sorted(list(set(stocks)))

print(f"\nScanning {len(stocks)} stocks using loss-minimization filters...", flush=True)

all_historical_signals = []

# ===== 4. OPTIMIZED BACKTEST LOOP =====
for stock in stocks:
    try:
        ticker_formatted = f"{stock}.NS"
        df = yf.download(ticker_formatted, period="1y", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        if df.empty or len(df) < 200: continue # Minimum 200 rows required for EMA
        
        df = build_indicators(df)
        total_rows = len(df)
        idx = 200 # Start after 200 EMA is ready
        
        while idx < total_rows:
            row = df.iloc[idx]
            if row['Close'] < R['min_price']:
                idx += 1
                continue
                
            is_signal, mode = check_price_action_at_index(df, idx)
            if is_signal:
                entry_price = row['Close']
                entry_date = df.index[idx]
                atr = row['ATR_14']
                
                # SUDHAR: Dynamic SL based on 1.5x ATR instead of fixed 5%
                sl_distance = atr * 1.5
                sl_price = entry_price - sl_distance
                
                # Target based on 1:2 Risk-Reward
                target_distance = sl_distance * R['target_r_multiple']
                target_price = entry_price + target_distance
                
                # Safety limits to avoid absurd bands
                if sl_price <= 0 or ((entry_price - sl_price)/entry_price) > 0.15:
                    idx += 1
                    continue
                
                result = "TIMEOUT"
                exit_date = None
                exit_price = entry_price
                
                exit_idx = idx + 1
                while exit_idx < min(idx + 1 + R['max_hold_days'], total_rows):
                    future_row = df.iloc[exit_idx]
                    if future_row['High'] >= target_price:
                        result = "WIN"
                        exit_date = df.index[exit_idx]
                        exit_price = target_price
                        break
                    elif future_row['Low'] <= sl_price:
                        result = "LOSS"
                        exit_date = df.index[exit_idx]
                        exit_price = sl_price
                        break
                    exit_price = future_row['Close']
                    exit_date = df.index[exit_idx]
                    exit_idx += 1
                
                pnl_pct = round((exit_price / entry_price - 1) * 100, 1)
                
                all_historical_signals.append({
                    'Stock': stock,
                    'Entry_Date': entry_date.strftime('%Y-%m-%d'),
                    'Entry': round(entry_price, 2),
                    'SL': round(sl_price, 2),
                    'Target': round(target_price, 2),
                    'Status': result,
                    'Exit_Date': exit_date.strftime('%Y-%m-%d') if exit_date else "N/A",
                    'Exit_Price': round(exit_price, 2),
                    'PnL_%': pnl_pct,
                    'Strategy_Mode': mode
                })
                idx = exit_idx + R['cooldown_days']
            else:
                idx += 1
                
        time.sleep(0.05)
    except Exception:
        continue

# ===== 5. EXPORT AND METRICS =====
if not all_historical_signals:
    print("\n⚠️ Alert: No optimized price action signals discovered.", flush=True)
else:
    df_results = pd.DataFrame(all_historical_signals)
    
    total_trades = len(df_results)
    wins = len(df_results[df_results['Status'] == 'WIN'])
    losses = len(df_results[df_results['Status'] == 'LOSS'])
    timeouts = len(df_results[df_results['Status'] == 'TIMEOUT'])
    winrate = round((wins / total_trades) * 100, 1) if total_trades else 0
    
    print("\n=======================================================")
    print("📢 OPTIMIZED STRATEGY PERFORMANCE REPORT 📢")
    print("=======================================================")
    print(f"Total Backtest Signals  : {total_trades}")
    print(f"Profitable Trades (Wins): {wins}")
    print(f"Stop Loss Trades (Loss): {losses}")
    print(f"Expired Trades (Timeout): {timeouts}")
    print(f"Strategy New Win Rate   : {winrate}%")
    print("=======================================================\n")
    
    try:
        # Update Sheets
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
            'Losses': losses,
            'Timeouts': timeouts
        }])
        ws_summary.update([summary_df.columns.values.tolist()] + summary_df.values.tolist())
        print("=== GSHEET METRICS UPDATED SUCCESSFULLY ===", flush=True)
    except Exception as e:
        print(f"❌ GSheet write error: {str(e)}", flush=True)

print(f"\n=== RUN COMPLETE ===", flush=True)
