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

print("=== POWER SPRING HYBRID V2.2 - WIN RATE OPTIMIZED ===", flush=True)
print(f"Backtest Period: {BACKTEST_START} to {BACKTEST_END}", flush=True)

# Google Sheets Setup
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# FIXED PARAMETERS
R = {
    'min_daily_value_cr': 0.3, 
    'sl_buffer_pct': 1.5,        
    'target_r': 2.0,             # Improved RR for better Profit Factor
    'max_risk_pct': 4.5, 
    'vol_blast_ratio': 1.0,      
    'adx_min': 20,               # Filter out choppy/sideways markets
    'rsi_min': 45,               # Ensure strong entry momentum
    'rsi_max': 75,               # Avoid buying at absolute overbought peaks
    '52h_proximity': 0.85,       # Must be within top 15% of 52W High (Strong Stocks Only)
    'time_stop_days': 10
}
S = {'spring_breach_pct': 0.01, 'spring_recover_pct': 0.005, 'max_spring_depth': 0.04}

def get_or_create_ws(sh, title):
    try: return sh.worksheet(title)
    except: return sh.add_worksheet(title=title, rows=10000, cols=30)

def calculate_indicators(df):
    df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
    df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()
    df['EMA200'] = df['Close'].ewm(span=200, adjust=False).mean()
    
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    high_low = df['High'] - df['Low']
    high_close = np.abs(df['High'] - df['Close'].shift())
    low_close = np.abs(df['Low'] - df['Close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    atr = true_range.rolling(14).mean()
    
    up_move = df['High'].diff()
    down_move = df['Low'].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
    plus_di = 100 * (pd.Series(plus_dm).rolling(14).mean() / atr)
    minus_di = 100 * (pd.Series(minus_dm).rolling(14).mean() / atr)
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
    df['ADX'] = dx.rolling(14).mean()
    return df

def check_power_swing(df, i, debug_counter):
    row = df.iloc[i]
    if pd.isna(row['EMA200']) or pd.isna(row['ADX']) or pd.isna(row['RSI']):
        debug_counter['nan'] += 1
        return False

    # Sequential filtering flow to track rejections correctly
    trend = row['Close'] > row['EMA20'] > row['EMA50'] > row['EMA200']
    if not trend:
        debug_counter['trend'] += 1
        return False

    pullback = row['Low'] <= row['EMA20'] * 1.05
    if not pullback:
        debug_counter['pullback'] += 1
        return False

    green = row['Close'] > row['Open']
    if not green:
        debug_counter['green'] += 1
        return False

    vol_avg = df['Volume'].iloc[max(0,i-20):i].mean()
    if pd.isna(vol_avg) or vol_avg < 1000:
        debug_counter['vol_avg'] += 1
        return False
        
    volume = row['Volume'] > vol_avg * R['vol_blast_ratio']
    if not volume:
        debug_counter['volume'] += 1
        return False

    rsi_ok = R['rsi_min'] <= row['RSI'] <= R['rsi_max']
    if not rsi_ok:
        debug_counter['rsi'] += 1
        return False

    adx_ok = row['ADX'] > R['adx_min']
    if not adx_ok:
        debug_counter['adx'] += 1
        return False

    return True

def check_spring_setup(df, i):
    if i < 2: return False
    row = df.iloc[i]
    prev = df.iloc[i-1]
    if pd.isna(prev['EMA20']): return False
    
    support = prev['EMA20']
    breached = prev['Low'] < support * (1 - S['spring_breach_pct'])
    not_too_deep = prev['Low'] > support * (1 - S['max_spring_depth'])
    recovered = row['Close'] > support * (1 + S['spring_recover_pct'])
    
    vol_avg = df['Volume'].iloc[max(0,i-20):i].mean()
    if pd.isna(vol_avg) or vol_avg < 1000: return False
    vol_confirm = row['Volume'] > vol_avg * 0.95
    
    return breached and not_too_deep and recovered and vol_confirm

def download_single_stock(stock):
    try:
        ticker = stock if stock.endswith('.NS') else f"{stock}.NS"
        df = yf.download(ticker, start=BACKTEST_START - timedelta(days=400),
                       end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True)
        if df.empty or len(df) < 200: return None, stock
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        df = calculate_indicators(df)
        df.index = df.index.strftime('%Y-%m-%d')
        df = df[~df.index.duplicated(keep='last')]
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

debug_counter = {'nan':0, 'trend':0, 'pullback':0, 'green':0, 'vol_avg':0, 'volume':0, 'rsi':0, 'adx':0, 'liquidity':0, '52h':0}
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

    print(f"Data ready for {len(stock_data)} stocks", flush=True)
    open_positions = []
    batch_trades = 0

    for current_date in date_range:
        # 1. Manage Open Positions
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
                pnl_rs = round((exit_price - pos['Entry']) * pos['Qty'], 0)
                all_trades.append({
                    'Stock': pos['Stock'], 'Category': pos['Category'],
                    'Entry_Date': pos['Entry_Date'], 'Exit_Date': current_date,
                    'Entry': pos['Entry'], 'Exit_Price': round(exit_price, 2),
                    'Status': exit_status, 'PnL_%': pnl_pct, 'PnL_Rs': pnl_rs,
                    'Days_Held': days_held
                })
                open_positions.remove(pos)
                batch_trades += 1

        # 2. Scan New Entries
        open_stocks = [p['Stock'] for p in open_positions]
        for stock, df in stock_data.items():
            if stock in open_stocks: continue
            if current_date not in df.index: continue
            
            i = df.index.get_loc(current_date)
            if i < 200: continue
            row = df.iloc[i]
            total_candles_checked += 1

            # Liquidity Filter
            avg_value_cr = (df['Close'].iloc[max(0,i-20):i] * df['Volume'].iloc[max(0,i-20):i]).mean() / 1e7
            if pd.isna(avg_value_cr) or avg_value_cr < R['min_daily_value_cr']:
                debug_counter['liquidity'] += 1
                continue

            # BUG FIX: Weak stocks filter hata kar strict 15% zone from 52W high kiya hai
            high_252 = df['High'].iloc[max(0,i-252):i].max()
            if not pd.isna(high_252) and row['Close'] < (high_252 * R['52h_proximity']):
                debug_counter['52h'] += 1
                continue

            # Check setups sequentially
            is_power_swing = check_power_swing(df, i, debug_counter)
            is_spring = check_spring_setup(df, i)

            if not is_power_swing and not is_spring:
                continue

            # Category Decision Logic
            if is_power_swing and is_spring:
                category = 'A'  
                sl_base = df['Low'].iloc[i-1]
            elif is_power_swing:
                category = 'B'  
                sl_base = row['EMA20'] * 0.98
            else:
                category = 'C'  
                sl_base = df['Low'].iloc[i-1]

            entry_price = row['Close']
            sl_price = sl_base * (1 - R['sl_buffer_pct']/100)
            risk = entry_price - sl_price
            risk_pct = risk / entry_price * 100
            
            if risk_pct > R['max_risk_pct'] or risk_pct <= 0: 
                continue
                
            target = entry_price + risk * R['target_r']
            qty = int(1000 / risk) if risk > 0 else 0  
            if qty == 0: continue

            open_positions.append({
                'Stock': stock, 'Category': category, 'Entry_Date': current_date,
                'Entry': round(entry_price, 2), 'SL': round(sl_price, 2),
                'Target': round(target, 2), 'Qty': qty
            })

    # Close Remaining Open Positions
    for pos in open_positions:
        df = stock_data[pos['Stock']]
        exit_price = df['Close'].iloc[-1]
        pnl_pct = round((exit_price / pos['Entry'] - 1) * 100, 1)
        pnl_rs = round((exit_price - pos['Entry']) * pos['Qty'], 0)
        all_trades.append({
            'Stock': pos['Stock'], 'Category': pos['Category'],
            'Entry_Date': pos['Entry_Date'], 'Exit_Date': BACKTEST_END.strftime('%Y-%m-%d'),
            'Entry': pos['Entry'], 'Exit_Price': round(exit_price, 2),
            'Status': 'TIME', 'PnL_%': pnl_pct, 'PnL_Rs': pnl_rs,
            'Days_Held': (BACKTEST_END - pd.to_datetime(pos['Entry_Date']).date()).days
        })

    print(f"Batch {batch_num + 1} complete | Trades in this batch: {batch_trades} | Total Trades: {len(all_trades)}", flush=True)

df_bt = pd.DataFrame(all_trades)

print("\n" + "="*60, flush=True)
print("DEBUG SUMMARY - KAUNSA FILTER KITNA REJECT KAR RAHA", flush=True)
print("="*60, flush=True)
print(f"Total Candles Checked: {total_candles_checked}", flush=True)
for k, v in debug_counter.items():
    print(f"Rejected by {k}: {v}", flush=True)

print("\n" + "="*60, flush=True)
print("FINAL RESULTS", flush=True)
print("="*60, flush=True)

if df_bt.empty:
    print("\nAbhi bhi 0 trades hain. Kripya check karein ki aapki Watchlist sheet khali toh nahi hai!", flush=True)
else:
    cat_mapping = {'A': 'Power+Spring Hybrid', 'B': 'Power Only', 'C': 'Spring Only'}
    for cat in ['A', 'B', 'C']:
        cat_df = df_bt[df_bt['Category'] == cat]
        if cat_df.empty:
            print(f"\nCategory {cat} ({cat_mapping[cat]}): No trades found.", flush=True)
            continue
        total = len(cat_df)
        wins = len(cat_df[cat_df['Status'] == 'WIN'])
        winrate = round(wins / total * 100, 1) if total else 0
        win_amt = cat_df[cat_df['Status']=='WIN']['PnL_Rs'].sum()
        loss_amt = abs(cat_df[cat_df['Status']=='LOSS']['PnL_Rs'].sum())
        pf = round(win_amt / loss_amt, 2) if loss_amt > 0 else 999
        print(f"\nCategory {cat} - {cat_mapping[cat]}")
        print(f"Total Trades: {total} | WinRate: {winrate}% | Profit Factor: {pf} | Net PnL: Rs.{cat_df['PnL_Rs'].sum():,.0f}", flush=True)

    print(f"\nCOMBINED SUMMARY: {len(df_bt)} Trades | Overall WR: {round(len(df_bt[df_bt['Status']=='WIN'])/len(df_bt)*100,1)}%", flush=True)

# Google Sheets Upload
try:
    ws_bt = get_or_create_ws(sh, "BACKTEST_HYBRID_1Y")
    ws_bt.clear()
    if not df_bt.empty:
        ws_bt.update([df_bt.columns.values.tolist()] + df_bt.values.tolist())
        print(f"\n[SUCCESS] Results successfully saved to Google Sheet 'BACKTEST_HYBRID_1Y'!", flush=True)
except Exception as e:
    print(f"GSheet upload error: {e}", flush=True)

print("\n=== BACKTEST COMPLETE ===", flush=True)
