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

print("=== POWER SPRING HYBRID V1 - NO PRICE FILTER ===", flush=True)
print(f"Backtest Period: {BACKTEST_START} to {BACKTEST_END}", flush=True)

gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# ===== FILTERS - PRICE KA KOI CHAKKAR HI NAHI =====
R = {
    # min_price: HATA DIYA - 5 rs ka bhi chalega agar liquidity hai
    # max_price: HATA DIYA - 10000 ka bhi chalega
    'min_daily_value_cr': 0.5, # SIRF LIQUIDITY MATTER KARTI HAI
    'sl_buffer_pct': 2.0, 'target_r': 1.5, 'max_risk_pct': 4.0,
    'vol_blast_ratio': 1.5, 'adx_min': 20, 'rsi_min': 45, 'rsi_max': 70,
    '52h_proximity': 0.85, # 0.88 se 0.85 kar diya - 15% tak neeche chalega
    'time_stop_days': 8
}

S = {'spring_breach_pct': 0.01, 'spring_recover_pct': 0.005, 'max_spring_depth': 0.03}

def get_or_create_ws(sh, title):
    try: return sh.worksheet(title)
    except: return sh.add_worksheet(title=title, rows=5000, cols=30)

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

def check_power_swing(df, i):
    row = df.iloc[i]
    if pd.isna(row['EMA200']) or pd.isna(row['ADX']) or pd.isna(row['RSI']): return False
    trend = row['Close'] > row['EMA20'] > row['EMA50'] > row['EMA200']
    pullback = row['Low'] <= row['EMA20'] * 1.02
    green = row['Close'] > row['Open']
    vol_avg = df['Volume'].iloc[i-20:i].mean()
    if vol_avg == 0 or pd.isna(vol_avg): return False
    volume = row['Volume'] > vol_avg * R['vol_blast_ratio']
    rsi_ok = R['rsi_min'] <= row['RSI'] <= R['rsi_max']
    adx_ok = row['ADX'] > R['adx_min']
    return trend and pullback and green and volume and rsi_ok and adx_ok

def check_spring_setup(df, i):
    if i < 2: return False
    row = df.iloc[i]
    prev = df.iloc[i-1]
    if pd.isna(prev['EMA20']): return False
    support = prev['EMA20']
    breached = prev['Low'] < support * (1 - S['spring_breach_pct'])
    not_too_deep = prev['Low'] > support * (1 - S['max_spring_depth'])
    recovered = row['Close'] > support * (1 + S['spring_recover_pct'])
    vol_avg = df['Volume'].iloc[i-20:i].mean()
    if vol_avg == 0 or pd.isna(vol_avg): return False
    vol_confirm = row['Volume'] > vol_avg * 1.2
    return breached and not_too_deep and recovered and vol_confirm

def download_single_stock(stock):
    try:
        df = yf.download(f"{stock}.NS", start=BACKTEST_START - timedelta(days=400),
                       end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        if len(df) < 300: return None, stock
        df = calculate_indicators(df)
        return df, stock
    except:
        return None, stock

# ===== MAIN BATCH LOGIC =====
all_trades = []
stocks = ws_watchlist.col_values(1)[1:]
stocks = sorted(list(set([s.strip().upper() for s in stocks if s.strip()])))
total_stocks = len(stocks)
total_batches = (total_stocks + BATCH_SIZE - 1) // BATCH_SIZE

print(f"\nTotal Watchlist: {total_stocks} stocks | Batches: {total_batches}", flush=True)
print("NOTE: Price filter removed. Penny stocks included if liquidity OK.", flush=True)
date_range = pd.date_range(BACKTEST_START, BACKTEST_END, freq='B')

for batch_num in range(total_batches):
    start_idx = batch_num * BATCH_SIZE
    end_idx = min(start_idx + BATCH_SIZE, total_stocks)
    batch_stocks = stocks[start_idx:end_idx]
    
    print(f"\n{'='*60}", flush=True)
    print(f"BATCH {batch_num + 1}/{total_batches} | Stocks {start_idx+1}-{end_idx}", flush=True)
    print(f"{'='*60}", flush=True)
    
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
        current_date = current_date.date()
        for pos in open_positions[:]:
            df = stock_data[pos['Stock']]
            if current_date not in df.index.date: continue
            row = df.loc[df.index.date == current_date].iloc[0]
            sl_hit = row['Low'] <= pos['SL']
            target_hit = row['High'] >= pos['Target']
            exit_price = None
            exit_status = None
            days_held = (current_date - pos['Entry_Date']).days
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
        
        open_stocks = [p['Stock'] for p in open_positions]
        for stock, df in stock_data.items():
            if stock in open_stocks: continue
            if current_date not in df.index.date: continue
            i = df.index.get_loc(df.index[df.index.date == current_date][0])
            if i < 300: continue
            row = df.iloc[i]
            
            # PRICE FILTER BILKUL HATA DIYA - SIRF LIQUIDITY CHECK
            avg_value_cr = (df['Close'].iloc[i-20:i] * df['Volume'].iloc[i-20:i]).mean() / 1e7
            if pd.isna(avg_value_cr) or avg_value_cr < R['min_daily_value_cr']: continue
            
            high_252 = df['High'].iloc[i-252:i].max()
            if pd.isna(high_252) or row['Close'] < high_252 * R['52h_proximity']: continue
            
            is_power_swing = check_power_swing(df, i)
            if not is_power_swing: continue
            
            is_spring = check_spring_setup(df, i)
            if is_power_swing and is_spring:
                category = 'A'
                sl_base = df['Low'].iloc[i-1]
            else:
                category = 'B'
                sl_base = row['EMA20'] * 0.98
            
            entry_price = row['Close']
            sl_price = sl_base * (1 - R['sl_buffer_pct']/100)
            risk = entry_price - sl_price
            risk_pct = risk / entry_price * 100
            if risk_pct > R['max_risk_pct'] or risk_pct <= 0: continue
            target = entry_price + risk * R['target_r']
            
            # QTY CALCULATION - PENNY ME BHI KAAM KAREGA
            qty = int(750 / risk) if risk > 0 else 0
            if qty == 0: continue
            
            open_positions.append({
                'Stock': stock, 'Category': category, 'Entry_Date': current_date,
                'Entry': round(entry_price, 2), 'SL': round(sl_price, 2),
                'Target': round(target, 2), 'Qty': qty
            })
    
    for pos in open_positions:
        df = stock_data[pos['Stock']]
        exit_price = df['Close'].iloc[-1]
        pnl_pct = round((exit_price / pos['Entry'] - 1) * 100, 1)
        pnl_rs = round((exit_price - pos['Entry']) * pos['Qty'], 0)
        all_trades.append({
            'Stock': pos['Stock'], 'Category': pos['Category'],
            'Entry_Date': pos['Entry_Date'], 'Exit_Date': BACKTEST_END,
            'Entry': pos['Entry'], 'Exit_Price': round(exit_price, 2),
            'Status': 'TIME', 'PnL_%': pnl_pct, 'PnL_Rs': pnl_rs,
            'Days_Held': (BACKTEST_END - pos['Entry_Date']).days
        })
    
    print(f"Batch {batch_num + 1} complete | Trades: {batch_trades} | Total: {len(all_trades)}", flush=True)

df_bt = pd.DataFrame(all_trades)

print("\n" + "="*60, flush=True)
print("FINAL RESULTS - NO PRICE FILTER", flush=True)
print("="*60, flush=True)

if df_bt.empty:
    print("\nAb bhi 0 trades? To strategy hi over-optimized hai", flush=True)
else:
    for cat in ['A', 'B']:
        cat_df = df_bt[df_bt['Category'] == cat]
        if cat_df.empty: continue
        total = len(cat_df)
        wins = len(cat_df[cat_df['Status'] == 'WIN'])
        winrate = round(wins / total * 100, 1) if total else 0
        win_amt = cat_df[cat_df['Status']=='WIN']['PnL_Rs'].sum()
        loss_amt = abs(cat_df[cat_df['Status']=='LOSS']['PnL_Rs'].sum())
        pf = round(win_amt / loss_amt, 2) if loss_amt > 0 else 999
        cat_name = "Power+Spring" if cat=='A' else "Power Only"
        print(f"\nCategory {cat} - {cat_name}", flush=True)
        print(f"Total: {total} | WR: {winrate}% | PF: {pf} | PnL: Rs.{cat_df['PnL_Rs'].sum():,.0f}", flush=True)

    print(f"\nCOMBINED: {len(df_bt)} Trades | WR: {round(len(df_bt[df_bt['Status']=='WIN'])/len(df_bt)*100,1)}%", flush=True)
    print(f"Avg Price of Trades: Rs.{df_bt['Entry'].mean():.0f}", flush=True)

try:
    ws_bt = get_or_create_ws(sh, "BACKTEST_HYBRID_1Y")
    ws_bt.clear()
    if not df_bt.empty:
        ws_bt.update([df_bt.columns.values.tolist()] + df_bt.values.tolist())
        print(f"\nSaved to GSHEET", flush=True)
except Exception as e:
    print(f"GSheet error: {e}", flush=True)

print("\n=== COMPLETE ===", flush=True)
