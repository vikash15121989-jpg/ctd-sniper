import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import warnings
warnings.filterwarnings('ignore')

BACKTEST_MODE = True
BACKTEST_END = datetime.now().date()
BACKTEST_START = BACKTEST_END - timedelta(days=365)
BATCH_SIZE = 50

print("=== RELATIVE STRENGTH BREAKOUT SNIPER V6.0 (RS 45+ ONLY) ===", flush=True)
print(f"Backtest Period: {BACKTEST_START} to {BACKTEST_END}", flush=True)

# Google Sheets Setup
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# BINA KOI DUSRA CRITERIA BADLE - SIRF RS >= 45 FILTER
R = {
    'min_daily_value_cr': 0.5,   # Minimum liquidity filter (50 Lakhs+)
    'fixed_target_pct': 6.0,     # Target 6%
    'fixed_sl_pct': 3.0,         # Stop Loss 3%
    'vol_blast_ratio': 1.5,      # Buyer Aggression: Volume 1.5x of 20-day average
    'rsi_min': 58,               # Strong momentum floor (RSI near 60)
    'rsi_max': 78,               # Avoid highly overbought stocks
    'min_rs_score': 45.0,        # CRITICAL: Strict Cutoff (Sirf RS 45 ke upar wale share)
    'time_stop_days': 10         # ORIGINAL TIME STOP (No change here)
}

def get_or_create_ws(sh, title):
    try: return sh.worksheet(title)
    except: return sh.add_worksheet(title=title, rows=10000, cols=30)

def calculate_indicators(df):
    # Trend Indicators
    df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
    df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()
    df['EMA200'] = df['Close'].ewm(span=200, adjust=False).mean()
    df['Vol_MA20'] = df['Volume'].rolling(window=20).mean()
    
    # RSI Calculation
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    return df

# Download Nifty 50 Data first for Relative Strength Comparison
print("\n[INFO] Downloading Nifty 50 reference data...", flush=True)
nifty_df = yf.download("^NSEI", start=BACKTEST_START - timedelta(days=400), end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True)
if isinstance(nifty_df.columns, pd.MultiIndex): nifty_df.columns = nifty_df.columns.get_level_values(0)
nifty_df.index = nifty_df.index.strftime('%Y-%m-%d')

def check_rs_breakout_entry(df, i, current_date, debug_counter):
    row = df.iloc[i]
    if pd.isna(row['EMA200']) or pd.isna(row['RSI']) or pd.isna(row['Vol_MA20']):
        debug_counter['nan'] += 1
        return False, 0.0

    # 1. Trend Filter: Buyer Aggression Check (EMA Alignment)
    trend = row['Close'] > row['EMA20'] > row['EMA50'] > row['EMA200']
    if not trend:
        debug_counter['trend'] += 1
        return False, 0.0

    # 2. Breakout Check: Current Close must be higher than previous 20-day high
    pichla_20_day_high = df['Close'].iloc[max(0, i-20):i].max()
    is_breakout = row['Close'] > pichla_20_day_high
    if not is_breakout:
        debug_counter['pullback'] += 1  
        return False, 0.0

    # 3. Volume Blast Check (Heavy Institutional Buying)
    if row['Vol_MA20'] < 1000 or row['Volume'] < (row['Vol_MA20'] * R['vol_blast_ratio']):
        debug_counter['volume'] += 1
        return False, 0.0

    # 4. Momentum Check (RSI Floor)
    rsi_ok = R['rsi_min'] <= row['RSI'] <= R['rsi_max']
    if not rsi_ok:
        debug_counter['rsi'] += 1
        return False, 0.0

    # 5. Relative Strength (RS) Calculation vs Nifty 50
    try:
        stock_start_p = df['Close'].iloc[i-20]
        stock_ret_20d = ((row['Close'] - stock_start_p) / stock_start_p) * 100
        
        nifty_idx = nifty_df.index.get_loc(current_date)
        nifty_start_p = nifty_df['Close'].iloc[nifty_idx-20]
        nifty_current_p = nifty_df['Close'].iloc[nifty_idx]
        nifty_ret_20d = ((nifty_current_p - nifty_start_p) / nifty_start_p) * 100
        
        rs_score = stock_ret_20d - nifty_ret_20d
        
        # Pure RS Score Filter
        if rs_score < R['min_rs_score']:
            debug_counter['adx'] += 1  
            return False, 0.0
    except:
        return False, 0.0

    return True, round(rs_score, 2)

def download_single_stock(stock):
    try:
        ticker = stock if stock.endswith('.NS') else f"{stock}.NS"
        df = yf.download(ticker, start=BACKTEST_START - timedelta(days=400),
                       end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True)
        if df.empty or len(df) < 200: return None, stock
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        df = calculate_indicators(df)
        df.index = df.index.strftime('%Y-%m-%d')
        return df, stock
    except:
        return None, stock

all_trades = []
stocks = ws_watchlist.col_values(1)[1:] 
stocks = sorted(list(set([s.strip().upper().replace('.NS','') for s in stocks if s.strip()])))
total_stocks = len(stocks)
total_batches = (total_stocks + BATCH_SIZE - 1) // BATCH_SIZE

print(f"\nTotal Watchlist: {total_stocks} stocks | Batches: {total_batches}", flush=True)
date_range = pd.date_range(BACKTEST_START, BACKTEST_END, freq='B').strftime('%Y-%m-%d')

debug_counter = {'nan':0, 'trend':0, 'pullback':0, 'green':0, 'vol_avg':0, 'volume':0, 'rsi':0, 'adx':0, 'liquidity':0}
total_candles_checked = 0

for batch_num in range(total_batches):
    start_idx = batch_num * BATCH_SIZE
    end_idx = min(start_idx + BATCH_SIZE, total_stocks)
    batch_stocks = stocks[start_idx:end_idx]

    print(f"\n{'='*60}", flush=True)
    print(f"BATCH {batch_num + 1}/{total_batches} | Stocks {start_idx+1}-{end_idx}", flush=True)

    stock_data = {}
    with ThreadPoolExecutor(max_workers=20) as executor:
        future_to_stock = {executor.submit(download_single_stock, stock): stock for stock in batch_stocks}
        for future in as_completed(future_to_stock):
            df, stock = future.result()
            if df is not None:
                stock_data[stock] = df

    open_positions = []
    
    for current_date in date_range:
        if current_date not in nifty_df.index: continue
        
        # Manage Open Positions
        for pos in open_positions[:]:
            df = stock_data[pos['Stock']]
            if current_date not in df.index: continue
            row = df.loc[current_date]
            
            sl_hit = row['Low'] <= pos['SL']
            target_hit = row['High'] >= pos['Target']
            exit_price = None
            exit_status = None
            days_held = (pd.to_datetime(current_date) - pd.to_datetime(pos['Entry_Date'])).days
            
            if sl_hit and target_hit:
                exit_price = pos['SL']; exit_status = 'LOSS'
            elif sl_hit:
                exit_price = pos['SL']; exit_status = 'LOSS'
            elif target_hit:
                exit_price = pos['Target']; exit_status = 'WIN'
            elif days_held >= R['time_stop_days']:
                exit_price = row['Close']; exit_status = 'TIME'
                
            if exit_price:
                pnl_pct = round((exit_price / pos['Entry'] - 1) * 100, 1)
                all_trades.append({
                    'Stock': pos['Stock'], 'Category': 'RS Breakout Swing',
                    'Entry_Date': pos['Entry_Date'], 'Exit_Date': current_date,
                    'Entry': pos['Entry'], 'Exit_Price': round(exit_price, 2),
                    'Status': exit_status, 'PnL_%': pnl_pct, 'Days_Held': days_held,
                    'RS_Score': pos['RS_Score']
                })
                open_positions.remove(pos)

        # Scan New Entries
        open_stocks = [p['Stock'] for p in open_positions]
        for stock, df in stock_data.items():
            if stock in open_stocks: continue
            if current_date not in df.index: continue
            
            i = df.index.get_loc(current_date)
            if i < 200: continue
            row = df.iloc[i]
            total_candles_checked += 1

            # Liquidity Lock
            avg_value_cr = (df['Close'].iloc[max(0,i-20):i] * df['Volume'].iloc[max(0,i-20):i]).mean() / 1e7
            if pd.isna(avg_value_cr) or avg_value_cr < R['min_daily_value_cr']:
                debug_counter['liquidity'] += 1
                continue

            # Check Relative Strength + Breakout logic
            is_entry, rs_score = check_rs_breakout_entry(df, i, current_date, debug_counter)
            if not is_entry:
                continue

            entry_price = row['Close']
            target_price = entry_price * (1 + R['fixed_target_pct']/100)
            sl_price = entry_price * (1 - R['fixed_sl_pct']/100)

            open_positions.append({
                'Stock': stock, 'Category': 'RS Breakout Swing', 'Entry_Date': current_date,
                'Entry': round(entry_price, 2), 'SL': round(sl_price, 2),
                'Target': round(target_price, 2), 'RS_Score': rs_score
            })

    # Close left-overs at the end of backtest period
    for pos in open_positions:
        df = stock_data[pos['Stock']]
        exit_price = df['Close'].iloc[-1]
        pnl_pct = round((exit_price / pos['Entry'] - 1) * 100, 1)
        all_trades.append({
            'Stock': pos['Stock'], 'Category': 'RS Breakout Swing',
            'Entry_Date': pos['Entry_Date'], 'Exit_Date': BACKTEST_END.strftime('%Y-%m-%d'),
            'Entry': pos['Entry'], 'Exit_Price': round(exit_price, 2),
            'Status': 'TIME', 'PnL_%': pnl_pct, 'Days_Held': (BACKTEST_END - pd.to_datetime(pos['Entry_Date']).date()).days,
            'RS_Score': pos['RS_Score']
        })

df_bt = pd.DataFrame(all_trades)

print("\n" + "="*60, flush=True)
print("FINAL RESULTS - PURE RS 45+ FILTER (ORIGINAL TIME STOP)", flush=True)
print("="*60, flush=True)

if df_bt.empty:
    print("\nNo breakout trades found with strict RS >= 45 rules.", flush=True)
else:
    total = len(df_bt)
    wins = len(df_bt[df_bt['Status'] == 'WIN'])
    losses = len(df_bt[df_bt['Status'] == 'LOSS'])
    times = len(df_bt[df_bt['Status'] == 'TIME'])
    winrate = round(wins / total * 100, 1) if total else 0
    
    print(f"Total Filtered Shares (RS >= 45): {total}")
    print(f"Total WIN Trades: {wins}")
    print(f"Total LOSS Trades: {losses}")
    print(f"Total TIME Exits (10 Days): {times}")
    print(f"⭐ CURRENT WIN-RATE (WITH RS 45+): {winrate}%")
    print("="*60, flush=True)

try:
    ws_bt = get_or_create_ws(sh, "RS_45_PLUS_BACKTEST")
    ws_bt.clear()
    if not df_bt.empty:
        cols = ['Stock', 'Category', 'Entry_Date', 'Exit_Date', 'Entry', 'Exit_Price', 'Status', 'PnL_%', 'Days_Held', 'RS_Score']
        df_bt = df_bt[cols]
        ws_bt.update([df_bt.columns.values.tolist()] + df_bt.values.tolist())
        print(f"\n[SUCCESS] Filtered List Saved to 'RS_45_PLUS_BACKTEST' Sheet!", flush=True)
except Exception as e:
    print(f"GSheet error: {e}", flush=True)
        
