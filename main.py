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

print("=== V19.3: CASE-INSENSITIVE BATCH-BASED SCANNER ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== 1. CONFIG =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")

# ⚠️ Aapki sheet me agar "Watchlist" ya "watchlist" kuch bhi likha ho, yeh handle kar lega
WATCHLIST_SHEET_NAME = "watchlist" 

R = {
    'min_price': 60,
    'max_hold_days': 30,
    'target_pct': 0.12,         # 12% Target
    'sl_loss_pct': 0.05,         # 5% StopLoss
    'lookback_trading_days': 10, # Active trades pichle 10 din ke andar ke
    'min_wr_for_vip': 0.50,      # 50% Win Rate
    'vip_min_trades': 4,
    'min_avg_volume': 500000,
    'min_daily_turnover': 30000000,
    'trailing_min_pct': 0.05,
    'trailing_max_pct': 0.12
}

# 🛠️ FIXED: Case-Insensitive Worksheet Matcher
def get_or_create_ws(sh, title, rows=1000, cols=15):
    try:
        # Saari available sheets check karein bina choti-badi abc ke bhedbhav ke
        for ws in sh.worksheets():
            if ws.title.strip().lower() == title.strip().lower():
                return ws
        # Agar bilkul nahi milti tabhi nayi sheet banayein
        return sh.add_worksheet(title=title, rows=rows, cols=cols)
    except Exception as e:
        print(f"⚠️ Error in get_or_create_ws for {title}: {e}", flush=True)
        # Fallback to standard approach
        try:
            return sh.worksheet(title)
        except:
            return sh.add_worksheet(title=title, rows=rows, cols=cols)

ws_watchlist = get_or_create_ws(sh, WATCHLIST_SHEET_NAME)
ws_filter = get_or_create_ws(sh, "HIGH_WINRATE_STOCKS")
ws_signals = get_or_create_ws(sh, "NEW_SIGNALS_TODAY")
ws_active = get_or_create_ws(sh, "ACTIVE_TRADES")

# ===== 2. LOAD TICKERS =====
def get_watchlist_tickers():
    try:
        all_rows = ws_watchlist.get_all_values()
        tickers = []
        for row in all_rows:
            if row and row[0].strip():
                val = row[0].strip().upper()
                if val in ["STOCK", "STOCK_NAME", "SYMBOL", "STOCKS"]: 
                    continue
                ticker_clean = val.split('.')[0]
                tickers.append(ticker_clean)
        return list(set(tickers))
    except Exception as e:
        print(f"❌ Error loading watchlist: {e}", flush=True)
        return []

# ===== 3. INDICATORS & LIQUIDITY =====
def build_indicators(df):
    if df.empty or len(df) < 35: return df
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    df.index = df.index.tz_localize(None) if df.index.tz is not None else df.index
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
        
    df['Breakout_High_20D'] = df['High'].shift(1).rolling(window=20).max()
    df['EMA_50'] = df['Close'].ewm(span=50, adjust=False).mean()
    df['Vol_20MA'] = df['Volume'].shift(1).rolling(window=20).mean()
    df['Vol_Multiple'] = df['Volume'] / (df['Vol_20MA'] + 1e-5)
    df['Turnover'] = df['Close'] * df['Volume']
    df['Turnover_20MA'] = df['Turnover'].shift(1).rolling(window=20).mean()
    return df

def check_liquidity(df, idx):
    if idx < 20 or len(df) <= idx: return False
    row = df.iloc[idx]
    return row['Vol_20MA'] >= R['min_avg_volume'] and row['Turnover_20MA'] >= R['min_daily_turnover'] and row['Close'] >= R['min_price']

# ===== 4. HISTORICAL BACKTEST LOGIC =====
def backtest_stock_winrate(df):
    total_trades = 0
    wins = 0
    breakout_indices = []
    
    if len(df) < 35: return 0.0, 0, []

    for idx in range(21, len(df)):
        row = df.iloc[idx]
        row_prev = df.iloc[idx-1]
        
        if pd.isna(row['EMA_50']) or pd.isna(row['Breakout_High_20D']): continue
        if not check_liquidity(df, idx): continue  
        if row['Close'] < row['EMA_50']: continue
        
        breakout_level = row['Breakout_High_20D']
        fresh_breakout = (row['Close'] > breakout_level) and (row_prev['Close'] <= breakout_level)
        good_volume = row['Vol_Multiple'] > 1.2
        is_green = row['Close'] > row['Open']
        
        if fresh_breakout and good_volume and is_green:
            breakout_indices.append(idx)
            
            entry_price = float(row['Close'])
            tgt_val = entry_price * (1 + R['target_pct'])
            sl_val = entry_price * (1 - R['sl_loss_pct'])
            
            df_future = df.iloc[idx+1 : idx+1 + R['max_hold_days']]
            if df_future.empty: continue
            
            tgt_hits = df_future[df_future['High'] >= tgt_val]
            sl_hits = df_future[df_future['Low'] <= sl_val]
            
            if not tgt_hits.empty and not sl_hits.empty:
                if tgt_hits.index[0] < sl_hits.index[0]: wins += 1
                total_trades += 1
            elif not tgt_hits.empty:
                wins += 1
                total_trades += 1
            elif not sl_hits.empty:
                total_trades += 1
                
    win_rate = wins / total_trades if total_trades > 0 else 0.0
    return win_rate, total_trades, breakout_indices

# ===== 5. MAIN EXECUTION WITH BATCHING =====
def main():
    today = datetime.now().date()
    all_tickers = get_watchlist_tickers()
    
    if not all_tickers:
        print("❌ Watchlist empty. Exiting...", flush=True)
        return
        
    print(f"Total Tickers Loaded from Watchlist Sheet: {len(all_tickers)}", flush=True)
    
    vip_stocks_data = []
    active_trades_pool = []
    live_signals_today = []
    
    # 50-50 Ke Batches
    batch_size = 50
    ticker_batches = [all_tickers[i:i + batch_size] for i in range(0, len(all_tickers), batch_size)]
    
    print(f"\n[PHASE 1] Starting Backtest in {len(ticker_batches)} batches...", flush=True)
    
    for batch_idx, batch in enumerate(ticker_batches, 1):
        print(f"Processing Batch {batch_idx}/{len(ticker_batches)} ({len(batch)} stocks)...", flush=True)
        
        tickers_string = " ".join([f"{t}.NS" for t in batch])
        try:
            batch_df = yf.download(tickers_string, period="2y", progress=False, auto_adjust=False)
        except Exception as e:
            print(f"⚠️ Batch {batch_idx} download failed: {e}. Skipping...", flush=True)
            continue
            
        for ticker in batch:
            try:
                if isinstance(batch_df.columns, pd.MultiIndex):
                    df = batch_df.xs(f"{ticker}.NS", axis=1, level=1).dropna(how='all')
                else:
                    df = batch_df.dropna(how='all')
                    
                if df.empty or len(df) < 35: continue
                
                df = build_indicators(df)
                win_rate, total_trades, breakout_indices = backtest_stock_winrate(df)
                
                # Filter: Win Rate > 50%
                if total_trades >= R['vip_min_trades'] and win_rate >= R['min_wr_for_vip']:
                    vip_stocks_data.append([ticker, f"{round(win_rate * 100, 2)}%", total_trades])
                    
                    if not breakout_indices: continue
                    
                    last_breakout_idx = breakout_indices[-1]
                    last_trading_idx = len(df) - 1
                    trading_days_since_breakout = last_trading_idx - last_breakout_idx
                    
                    row_sig = df.iloc[last_breakout_idx]
                    sig_date = df.index[last_breakout_idx].date()
                    entry_price = round(float(row_sig['Close']), 2)
                    tgt_price = round(entry_price * (1 + R['target_pct']), 2)
                    sl_price = round(entry_price * (1 - R['sl_loss_pct']), 2)
                    
                    df_after_signal = df.iloc[last_breakout_idx + 1:]
                    
                    # 1. LIVE SIGNAL TODAY (Aaj action, kal entry)
                    if last_breakout_idx == last_trading_idx:
                        live_signals_today.append({
                            'Stock_Name': ticker, 'Signal_Date': sig_date.strftime('%Y-%m-%d'),
                            'Entry_Price': entry_price, 'StopLoss_Price': sl_price, 'Target_Price': tgt_price
                        })
                    
                    # 2. ACTIVE TRADES (Pichle 10 trading dino ke andar price action hua hai)
                    if trading_days_since_breakout <= R['lookback_trading_days'] and last_breakout_idx != last_trading_idx:
                        if df_after_signal.empty: continue
                        
                        max_high = float(df_after_signal['High'].max().astype(float).item())
                        min_low = float(df_after_signal['Low'].min().astype(float).item())
                        max_pct_from_entry = round(((max_high - entry_price) / entry_price) * 100, 2)
                        
                        tgt_hits = df_after_signal[df_after_signal['High'] >= tgt_price]
                        sl_hits = df_after_signal[df_after_signal['Low'] <= sl_price]
                        
                        status = "OPEN"
                        exit_date_str = ""
                        
                        if not tgt_hits.empty and not sl_hits.empty:
                            if tgt_hits.index[0] < sl_hits.index[0]:
                                status, exit_date_str = "PROFIT", tgt_hits.index[0].date().strftime('%Y-%m-%d')
                            else:
                                status, exit_date_str = "LOSS", sl_hits.index[0].date().strftime('%Y-%m-%d')
                        elif not tgt_hits.empty:
                            status, exit_date_str = "PROFIT", tgt_hits.index[0].date().strftime('%Y-%m-%d')
                        elif not sl_hits.empty:
                            status, exit_date_str = "LOSS", sl_hits.index[0].date().strftime('%Y-%m-%d')
                        else:
                            if (today - sig_date).days >= R['max_hold_days']:
                                status, exit_date_str = "TIMEOUT", today.strftime('%Y-%m-%d')
                            elif max_pct_from_entry < (R['trailing_min_pct'] * 100):
                                status = "OPEN"
                            else:
                                status = "TRAILING"
                                
                        active_trades_pool.append({
                            'Stock_Name': ticker, 'Signal_Date': sig_date.strftime('%Y-%m-%d'),
                            'Entry_Price': entry_price, 'Target_Price': tgt_price, 'StopLoss_Price': sl_price,
                            'Exit_Date': exit_date_str, 'Status': status, 'PCT_FROM_ENTRY': max_pct_from_entry
                        })
            except Exception:
                pass 
                
        time.sleep(1)

    # ===== PHASE 4: GOOGLE SHEETS SYNC =====
    print("\n[PHASE 2] Syncing data back to Google Sheets...", flush=True)
    
    # 1. HIGH_WINRATE_STOCKS Update
    try:
        ws_filter.clear()
        vip_headers = ['Stock_Name', 'Win_Rate', 'Total_Historical_Trades']
        if vip_stocks_data:
            ws_filter.update(values=[vip_headers] + vip_stocks_data, range_name='A1')
        print(f"🎯 VIP Sheet Updated: {len(vip_stocks_data)} Stocks", flush=True)
    except Exception as e: print(f"❌ Error updating VIP Sheet: {e}")

    # 2. ACTIVE_TRADES Update
    try:
        ws_active.clear()
        master_headers = ['Stock_Name', 'Signal_Date', 'Entry_Price', 'Target_Price', 'StopLoss_Price', 'Exit_Date', 'Status', 'PCT_FROM_ENTRY']
        if active_trades_pool:
            df_active = pd.DataFrame(active_trades_pool).sort_values(by='Signal_Date', ascending=False)
            ws_active.update(values=[master_headers] + df_active.values.tolist(), range_name='A1')
        print(f"📈 Active Trades Sheet Updated: {len(active_trades_pool)} Stocks", flush=True)
    except Exception as e: print(f"❌ Error updating Active Sheet: {e}")

    # 3. NEW_SIGNALS_TODAY Update
    try:
        ws_signals.clear()
        signal_headers = ['Stock_Name', 'Signal_Date', 'Entry_Price', 'StopLoss_Price', 'Target_Price']
        if live_signals_today:
            df_sig = pd.DataFrame(live_signals_today)
            ws_signals.update
            
