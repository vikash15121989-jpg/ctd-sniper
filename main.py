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

print("=== RS BEATER V24 - THE 6% TARGET SNIPER ===", flush=True)
print(f"Backtest Period: {BACKTEST_START} to {BACKTEST_END}", flush=True)

gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# V24 PARAMETERS OPTIMIZED FOR STABLE 6% SWING MOVES
R = {
    'min_daily_value_cr': 30.0,    
    'trend_days': 20,              
    'fixed_target_pct': 6.0,       # TAILORED TO YOUR 6% TARGET REQUIREMENT
    'fixed_sl_pct': 3.0,          # Tight 3% Stop Loss
    'time_stop_days': 8,           
    'risk_per_trade': 10000,       
    'cooldown_days': 5,            
    'max_open_trades': 6,          
    'rs_1m_min': 2.0,              
}

def get_or_create_ws(sh, title):
    try: return sh.worksheet(title)
    except: return sh.add_worksheet(title=title, rows=10000, cols=30)

def calculate_indicators(df):
    df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
    df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()
    df['Vol_MA20'] = df['Volume'].rolling(window=20).mean()
    
    df['MaxVol_10D'] = df['Volume'].shift(1).rolling(window=10).max()
    df['MaxHigh_10D'] = df['High'].shift(1).rolling(window=10).max()

    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    return df

print("\n Downloading Nifty 50 reference data...", flush=True)
nifty_df = yf.download("^NSEI", start=BACKTEST_START - timedelta(days=100), end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True)
if isinstance(nifty_df.columns, pd.MultiIndex): nifty_df.columns = nifty_df.columns.get_level_values(0)
nifty_df.index = pd.to_datetime(nifty_df.index).strftime('%Y-%m-%d')
nifty_df = nifty_df[~nifty_df.index.duplicated(keep='last')]

def calculate_rs_1m(df, i, current_date, nifty_df):
    try:
        if current_date not in nifty_df.index or i < 21: return None
        nifty_idx = nifty_df.index.get_loc(current_date)
        if nifty_idx < 21: return None

        stock_start = df['Close'].iloc[i-21]
        stock_now = df['Close'].iloc[i]
        stock_ret = ((stock_now - stock_start) / stock_start) * 100

        nifty_start = nifty_df['Close'].iloc[nifty_idx-21]
        nifty_now = nifty_df['Close'].iloc[nifty_idx]
        nifty_ret = ((nifty_now - nifty_start) / nifty_start) * 100

        return round(stock_ret - nifty_ret, 2)
    except: return None

def check_compression_and_breakout(df, current_idx, debug_counter):
    row_today = df.iloc[current_idx]
    if not (row_today['Close'] > row_today['EMA20'] > row_today['EMA50']):
        debug_counter['trend'] += 1
        return False, 0
    if not (53 <= row_today['RSI'] <= 75):
        debug_counter['rsi'] += 1
        return False, 0

    setup_found = False
    trigger_level = 0
    for lookback in range(1, 11):
        setup_idx = current_idx - lookback
        if setup_idx < 20: break
        row_setup = df.iloc[setup_idx]
        if row_setup['Volume'] > row_setup['MaxVol_10D'] and row_setup['High'] < row_setup['MaxHigh_10D']:
            setup_found = True
            trigger_level = row_setup['MaxHigh_10D']
            break
            
    if not setup_found:
        debug_counter['no_base'] += 1 
        return False, 0

    if row_today['Close'] > trigger_level and df['Close'].iloc[current_idx-1] <= trigger_level:
        if row_today['Volume'] < (row_today['Vol_MA20'] * 1.2):
            debug_counter['volume'] += 1
            return False, 0
        candle_range = row_today['High'] - row_today['Low']
        if candle_range > 0:
            upper_wick = row_today['High'] - max(row_today['Open'], row_today['Close'])
            if (upper_wick / candle_range) > 0.25:
                debug_counter['no_breakout'] += 1 
                return False, 0
        return True, trigger_level

    debug_counter['no_breakout'] += 1
    return False, 0

def download_single_stock(stock):
    try:
        ticker = stock if stock.endswith('.NS') else f"{stock}.NS"
        df = yf.download(ticker, start=BACKTEST_START - timedelta(days=100),
                       end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True)
        if df.empty or len(df) < 50: return None, stock
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        df = calculate_indicators(df)
        df.index = pd.to_datetime(df.index).strftime('%Y-%m-%d')
        df = df[~df.index.duplicated(keep='last')]
        return df, stock
    except: return None, stock

all_trades = []
stocks = ws_watchlist.col_values(1)[1:]
stocks = sorted(list(set([s.strip().upper().replace('.NS','') for s in stocks if s.strip()])))
total_stocks = len(stocks)
total_batches = (total_stocks + BATCH_SIZE - 1) // BATCH_SIZE

date_range = pd.date_range(BACKTEST_START, BACKTEST_END, freq='B').strftime('%Y-%m-%d')
debug_counter = {'nan':0, 'trend':0, 'no_base':0, 'no_breakout':0, 'volume':0, 'rsi':0, 'rs_weak':0, 'liquidity':0, 'cooldown':0, 'max_positions':0}
last_exit_dates = {}

# Performance tracking dictionary
stock_perf = {}

for batch_num in range(total_batches):
    start_idx = batch_num * BATCH_SIZE
    end_idx = min(start_idx + BATCH_SIZE, total_stocks)
    batch_stocks = stocks[start_idx:end_idx]

    print(f"Processing Batch {batch_num + 1}/{total_batches}...", flush=True)

    stock_data = {}
    with ThreadPoolExecutor(max_workers=20) as executor:
        future_to_stock = {executor.submit(download_single_stock, stock): stock for stock in batch_stocks}
        for future in as_completed(future_to_stock):
            df, stock = future.result()
            if df is not None: stock_data[stock] = df

    open_positions = []

    for current_date in date_range:
        current_dt = pd.to_datetime(current_date).date()

        for pos in open_positions[:]:
            df = stock_data[pos['Stock']]
            if current_date not in df.index: continue
            row = df.loc[current_date]

            # DYNAMIC COCH-TO-COST TRAILING AT 3% PROFIT
            current_max_profit = ((row['High'] / pos['Entry']) - 1) * 100
            current_sl = pos['SL']
            if current_max_profit >= 3.0:
                current_sl = pos['Entry']

            sl_hit = row['Low'] <= current_sl
            target_hit = row['High'] >= pos['Target']
            exit_price = None
            exit_status = None
            days_held = (current_dt - pd.to_datetime(pos['Entry_Date']).date()).days

            if sl_hit and target_hit:
                exit_price = current_sl; exit_status = 'LOSS'
            elif target_hit:
                exit_price = pos['Target']; exit_status = 'WIN'
            elif sl_hit:
                exit_price = current_sl; exit_status = 'LOSS'
            elif days_held >= R['time_stop_days']:
                exit_price = row['Close']; exit_status = 'TIME'

            if exit_price:
                pnl_pct = round((exit_price / pos['Entry'] - 1) * 100, 1)
                pnl_rs = round((exit_price - pos['Entry']) * pos['Qty'], 0)
                
                s = pos['Stock']
                if s not in stock_perf: 
                    stock_perf[s] = {'Wins': 0, 'Losses': 0, 'Total_Trades': 0, 'Total_PnL': 0}
                
                stock_perf[s]['Total_Trades'] += 1
                stock_perf[s]['Total_PnL'] += pnl_rs
                if exit_status == 'WIN': stock_perf[s]['Wins'] += 1
                elif exit_status == 'LOSS': stock_perf[s]['Losses'] += 1

                all_trades.append({
                    'Stock': pos['Stock'], 'Entry_Date': pos['Entry_Date'], 'Exit_Date': current_date,
                    'Entry': pos['Entry'], 'Exit_Price': round(exit_price, 2),
                    'Status': exit_status, 'PnL_%': pnl_pct, 'PnL_Rs': pnl_rs, 'Days_Held': days_held
                })
                last_exit_dates[pos['Stock']] = current_dt
                open_positions.remove(pos)

        if len(open_positions) >= R['max_open_trades']: continue

        open_stocks = [p['Stock'] for p in open_positions]
        for stock, df in stock_data.items():
            if stock in open_stocks: continue
            if stock in last_exit_dates:
                if (current_dt - last_exit_dates[stock]).days < R['cooldown_days']: continue
            if current_date not in df.index: continue

            i = df.index.get_loc(current_date)
            if i < 50: continue
            row = df.iloc[i]

            avg_value_cr = (df['Close'].iloc[max(0,i-20):i] * df['Volume'].iloc[max(0,i-20):i]).mean() / 1e7
            if pd.isna(avg_value_cr) or avg_value_cr < R['min_daily_value_cr']: continue

            is_entry, trigger_level = check_compression_and_breakout(df, i, debug_counter)
            if not is_entry: continue

            rs_1m = calculate_rs_1m(df, i, current_date, nifty_df)
            if rs_1m is None or rs_1m < R['rs_1m_min']: continue

            entry_price = row['Close']
            target_price = entry_price * (1 + (R['fixed_target_pct'] / 100))
            sl_price = entry_price * (1 - (R['fixed_sl_pct'] / 100))
            
            risk_per_share = entry_price - sl_price
            qty = int(R['risk_per_trade'] / risk_per_share) if risk_per_share > 0 else 0
            if qty == 0: continue

            open_positions.append({
                'Stock': stock, 'Entry_Date': current_date, 'Entry': round(entry_price, 2),
                'SL': round(sl_price, 2), 'Target': round(target_price, 2), 'Qty': qty
            })

# STRICT 6% TARGET LEADERBOARD ANALYSIS
df_perf = pd.DataFrame.from_dict(stock_perf, orient='index').reset_index().rename(columns={'index': 'Stock'})
if not df_perf.empty:
    df_perf['Win_Rate_%'] = round((df_perf['Wins'] / df_perf['Total_Trades']) * 100, 1)
    
    # Filter out illiquid or low frequency backtest samples (Min 2 trades require kiya hai optimize list ke liye)
    df_perf = df_perf[df_perf['Total_Trades'] >= 2]
    df_perf = df_perf.sort_values(by=['Wins', 'Win_Rate_%'], ascending=[False, False])

print("\n" + "="*60)
print("🏆 REFINED 6% SWING LEADEDBOARD (MIN 2 TRADES) 🏆")
print("="*60)
if df_perf.empty:
    print("Is filter par abhi koi stock match nahi hua. Watchlist badhani padegi.")
else:
    print(df_perf.head(10).to_string(index=False))
    print("="*60)

try:
    ws_bt = get_or_create_ws(sh, "20EMA_BREAKOUT_BT")
    ws_bt.clear()
    df_bt = pd.DataFrame(all_trades)
    if not df_bt.empty:
        ws_bt.update([df_bt.columns.values.tolist()] + df_bt.values.tolist())
        print(f"\n[SUCCESS] Results saved to '20EMA_BREAKOUT_BT' Sheet!", flush=True)
except Exception as e:
    print(f"GSheet error: {e}", flush=True)
    
