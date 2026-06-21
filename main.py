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

print("=== PURE PRICE ACTION BACKTEST ENGINE V8.3 ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== 1. SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# Backtest Core Configuration
R = {
    'min_price': 50,
    'max_hold_days': 30,    # Max 30 days hold
    'cooldown_days': 10,
    'target_pct': 0.10,     # 10% Profit Target
    'sl_loss_pct': 0.05,     # 5% Stop Loss
}

today = datetime.now().date()

# ===== 2. LOAD EXISTING POSITIONS FUNCTION =====
def get_or_create_ws(sh, title):
    try: return sh.worksheet(title)
    except: return sh.add_worksheet(title=title, rows=1000, cols=30)

ws_live = get_or_create_ws(sh, "LIVE_TRADES_V8_3")
try:
    df_live = pd.DataFrame(ws_live.get_all_records())
    if df_live.empty: df_live = pd.DataFrame(columns=['Stock','Entry_Date','Entry','SL','Target','Qty','Status','Exit_Date','Exit_Price','PnL_%','PnL_Rs'])
except:
    df_live = pd.DataFrame(columns=['Stock','Entry_Date','Entry','SL','Target','Qty','Status','Exit_Date','Exit_Price','PnL_%','PnL_Rs'])

open_trades = df_live[df_live['Status'] == 'OPEN'].copy() if not df_live.empty else pd.DataFrame()

# ===== 3. PRICE ACTION TECHNICAL INDICATORS =====
def build_indicators(df):
    if len(df) < 21: return df
    df['Support_20D'] = df['Low'].shift(1).rolling(window=20).min()
    df['Resistance_10D'] = df['High'].shift(1).rolling(window=10).max()
    df['Vol_20MA'] = df['Volume'].shift(1).rolling(window=20).mean()
    df['Vol_Multiple'] = df['Volume'] / (df['Vol_20MA'] + 1e-5)
    return df

def check_price_action_at_index(df, idx):
    if idx < 1: return False, None
    
    row = df.iloc[idx]
    row_prev = df.iloc[idx-1]
    is_green = row['Close'] > row['Open']
    
    # STRATEGY 1: PA_SUPPORT_RETEST
    low_near_support = ((row['Low'] / row['Support_20D']) - 1) * 100 <= 2.0
    body = abs(row['Close'] - row['Open'])
    lower_wick = min(row['Open'], row['Close']) - row['Low']
    strong_rejection = lower_wick >= (body * 1.0)
    
    if low_near_support and (strong_rejection or is_green):
        return True, "PA_SUPPORT_RETEST"
        
    # STRATEGY 2: PA_CHoCH_BREAKOUT
    broke_resistance = row['Close'] > row['Resistance_10D'] and row_prev['Close'] <= row_prev['Resistance_10D']
    strong_volume = row['Vol_Multiple'] > 1.25
    
    if broke_resistance and strong_volume and is_green:
        return True, "PA_CHoCH_BREAKOUT"
        
    return False, None

# ===== 4. READ WATCHLIST STOCKS & RUN BACKTEST =====
stocks = ws_watchlist.col_values(1)[1:]
stocks = sorted(list(set([s.strip().upper() for s in stocks if s.strip()])))

if stocks and stocks[0] in ['SYMBOL', 'TICKER', 'STOCKS', 'STOCK']:
    stocks = stocks[1:]

print(f"\nRunning Price Action Backtest on {len(stocks)} stocks from your Watchlist...", flush=True)

all_historical_signals = []

for stock in stocks:
    try:
        ticker_formatted = f"{stock}.NS" if not stock.endswith(".NS") else stock
        df = yf.download(ticker_formatted, period="1y", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        if df.empty or len(df) < 30: continue
        
        df = build_indicators(df)
        total_rows = len(df)
        idx = 21
        
        # Loop over historical rows to find and track strategy entries
        while idx < total_rows:
            row = df.iloc[idx]
            if row['Close'] < R['min_price']:
                idx += 1
                continue
                
            is_signal, mode = check_price_action_at_index(df, idx)
            if is_signal:
                entry_price = row['Close']
                entry_date = df.index[idx]
                
                target_price = entry_price * (1 + R['target_pct'])
                sl_price = entry_price * (1 - R['sl_loss_pct'])
                
                result = "TIMEOUT"
                exit_date = None
                exit_price = entry_price
                
                # Forward trace for Target/SL simulation
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
                    'Qty': 10,
                    'Status': result,
                    'Exit_Date': exit_date.strftime('%Y-%m-%d') if exit_date else "N/A",
                    'Exit_Price': round(exit_price, 2),
                    'PnL_%': pnl_pct,
                    'PnL_Rs': round(pnl_pct * 10, 0),
                    'Strategy_Mode': mode
                })
                idx = exit_idx + R['cooldown_days']
            else:
                idx += 1
                
        time.sleep(0.05)
    except Exception:
        continue

# ===== 5. EXPORT RESULTS AND GENERATE WIN RATE REPORT =====
if not all_historical_signals:
    print("\n⚠️ Alert: No price action signals discovered across the watchlist dataset.", flush=True)
else:
    df_results = pd.DataFrame(all_historical_signals)
    
    total_trades = len(df_results)
    wins = len(df_results[df_results['Status'] == 'WIN'])
    losses = len(df_results[df_results['Status'] == 'LOSS'])
    timeouts = len(df_results[df_results['Status'] == 'TIMEOUT'])
    winrate = round((wins / total_trades) * 100, 1) if total_trades else 0
    
    print("\n=======================================================")
    print("📢 STRATEGY WINNING RATE METRICS REPORT 📢")
    print("=======================================================")
    print(f"Total Backtest Signals  : {total_trades}")
    print(f"Profitable Trades (Wins): {wins}")
    print(f"Stop Loss Trades (Loss): {losses}")
    print(f"Expired Trades (Timeout): {timeouts}")
    print(f"Strategy Master Win Rate: {winrate}%")
    print("=======================================================\n")
    
    try:
        # 1. LIVE_TRADES tab par report write karna
        ws_live.clear()
        cols_to_push = ['Stock','Entry_Date','Entry','SL','Target','Qty','Status','Exit_Date','Exit_Price','PnL_%','PnL_Rs']
        df_sheet = df_results[cols_to_push].fillna("")
        ws_live.update([df_sheet.columns.values.tolist()] + df_sheet.values.tolist())
        
        # 2. LIVE_SUMMARY tab create/update karna
        ws_summary = get_or_create_ws(sh, "LIVE_SUMMARY")
        ws_summary.clear()
        
        summary_df = pd.DataFrame([{
            'Execution_Date': datetime.now().strftime('%Y-%m-%d'),
            'Total_Backtest_Trades': total_trades,
            'Strategy_Winrate_%': winrate,
            'Total_Wins': wins,
            'Total_Losses': losses,
            'Total_Timeouts': timeouts
        }])
        ws_summary.update([summary_df.columns.values.tolist()] + summary_df.values.tolist())
        
        # 3. NEW_SIGNALS_TODAY tab update karna (Isme sirf last strategy mode validation save hoga)
        ws_signals = get_or_create_ws(sh, "NEW_SIGNALS_TODAY")
        ws_signals.clear()
        df_signals_push = df_results[['Stock', 'Entry_Date', 'Entry', 'SL', 'Target', 'Strategy_Mode']].fillna("")
        ws_signals.update([df_signals_push.columns.values.tolist()] + df_signals_push.values.tolist())
        
        print("=== GSHEET BACKTEST METRICS UPDATED SUCCESSFULLY ===", flush=True)
    except Exception as e:
        print(f"❌ GSheet write error: {str(e)}", flush=True)

print(f"\n=== LIVE RUN COMPLETE ===", flush=True)
