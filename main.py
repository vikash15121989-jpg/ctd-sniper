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

BACKTEST_END = datetime.now().date()
BACKTEST_START = BACKTEST_END - timedelta(days=365)
BATCH_SIZE = 50

print("=== RS BEATER V27 - 4-STRATEGY COMPARATIVE ENGINE ===", flush=True)
print(f"Testing Period: {BACKTEST_START} to {BACKTEST_END}\n", flush=True)

gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

R = {
    'min_daily_value_cr': 30.0,
    'fixed_target_pct': 6.0,       # Strict 6% Target
    'fixed_sl_pct': 3.0,           # Strict 3% SL
    'time_stop_days': 8,
    'cooldown_days': 4
}

def calculate_base_indicators(df):
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    df['Low_Min_10D'] = df['Low'].shift(1).rolling(window=10).min()
    return df

# --- 4 INDIVIDUAL PATTERN ENGINES ---
def check_fvg(df, idx):
    if idx < 2: return False
    # Today's Low is greater than 2-days-ago High (Bullish FVG Void)
    return df['Low'].iloc[idx] > df['High'].iloc[idx-2]

def check_stophunt(df, idx):
    if idx < 1: return False
    row_today = df.iloc[idx]
    # Low broke 10D low, but Close sustained above it
    if (row_today['Low'] < row_today['Low_Min_10D']) and (row_today['Close'] > row_today['Low_Min_10D']):
        candle_range = row_today['High'] - row_today['Low']
        if candle_range > 0:
            lower_wick = min(row_today['Open'], row_today['Close']) - row_today['Low']
            return (lower_wick / candle_range) >= 0.35
    return False

def check_accumulation(df, idx):
    if idx < 10: return False
    price_flat_or_down = df['Close'].iloc[idx] <= df['Close'].iloc[idx-10]
    rsi_rising = df['RSI'].iloc[idx] > df['RSI'].iloc[idx-10]
    return price_flat_or_down and rsi_rising

def check_sandwich(df, idx):
    if idx < 2: return False
    c_today = df.iloc[idx]      # Right Bread
    c_prev = df.iloc[idx-1]     # Stuffing
    c_prev2 = df.iloc[idx-2]    # Left Bread
    
    # 1. Left is Bullish, Middle is a minor pause/red, Right is strong Bullish Breakout
    is_left_bull = c_prev2['Close'] > c_prev2['Open']
    is_right_bull = c_today['Close'] > c_today['Open']
    
    # 2. Middle candle stays within bounds (No breakdown below left low)
    middle_sustained = c_prev['Low'] >= c_prev2['Low']
    
    # 3. Right candle engulfs/breaks out above the middle candle's high
    sandwich_breakout = c_today['Close'] > c_prev['High']
    
    return is_left_bull and is_right_bull and middle_sustained and sandwich_breakout

def download_single_stock(stock):
    try:
        ticker = stock if stock.endswith('.NS') else f"{stock}.NS"
        df = yf.download(ticker, start=BACKTEST_START - timedelta(days=50),
                       end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True)
        if df.empty or len(df) < 30: return None, stock
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        df = calculate_base_indicators(df)
        df.index = pd.to_datetime(df.index).strftime('%Y-%m-%d')
        df = df[~df.index.duplicated(keep='last')]
        return df, stock
    except: return None, stock

# Load all watchlists
stocks = ws_watchlist.col_values(1)[1:]
stocks = sorted(list(set([s.strip().upper().replace('.NS','') for s in stocks if s.strip()])))
total_stocks = len(stocks)
total_batches = (total_stocks + BATCH_SIZE - 1) // BATCH_SIZE

strategies = {
    'Strat_1_FVG': check_fvg,
    'Strat_2_StopHunt': check_stophunt,
    'Strat_3_HiddenAccum': check_accumulation,
    'Strat_4_Sandwich': check_sandwich
}

# Master performance tracker
perf_summary = {strat: {'Wins':0, 'Losses':0, 'Total':0, 'PnL':0.0} for strat in strategies}

date_range = pd.date_range(BACKTEST_START, BACKTEST_END, freq='B').strftime('%Y-%m-%d')

for batch_num in range(total_batches):
    start_idx = batch_num * BATCH_SIZE
    end_idx = min(start_idx + BATCH_SIZE, total_stocks)
    batch_stocks = stocks[start_idx:end_idx]

    stock_data = {}
    with ThreadPoolExecutor(max_workers=20) as executor:
        future_to_stock = {executor.submit(download_single_stock, stock): stock for stock in batch_stocks}
        for future in as_completed(future_to_stock):
            df, stock = future.result()
            if df is not None: stock_data[stock] = df

    # Run backtest independently for each strategy to avoid mix-up
    for strat_name, strat_func in strategies.items():
        open_positions = {}
        last_exit_idx = {}

        for current_date in date_range:
            for stock, df in stock_data.items():
                if current_date not in df.index: continue
                idx = df.index.get_loc(current_date)
                row = df.iloc[idx]
                
                # Liquidity check
                avg_val = (df['Close'].iloc[max(0,idx-20):idx] * df['Volume'].iloc[max(0,idx-20):idx]).mean() / 1e7
                if pd.isna(avg_val) or avg_val < R['min_daily_value_cr']: continue

                # Position Management
                if stock in open_positions:
                    pos = open_positions[stock]
                    
                    # Trailing at 3% profit
                    current_max_profit = ((row['High'] / pos['Entry']) - 1) * 100
                    current_sl = pos['SL']
                    if current_max_profit >= 3.0:
                        current_sl = pos['Entry']

                    sl_hit = row['Low'] <= current_sl
                    target_hit = row['High'] >= pos['Target']
                    days_held = idx - pos['Entry_Idx']

                    exit_price = None
                    if sl_hit and target_hit: exit_price = current_sl
                    elif target_hit: exit_price = pos['Target']
                    elif sl_hit: exit_price = current_sl
                    elif days_held >= R['time_stop_days']: exit_price = row['Close']

                    if exit_price:
                        pnl_pct = (exit_price / pos['Entry'] - 1) * 100
                        perf_summary[strat_name]['Total'] += 1
                        if pnl_pct > 0.1:
                            perf_summary[strat_name]['Wins'] += 1
                        else:
                            perf_summary[strat_name]['Losses'] += 1
                        perf_summary[strat_name]['PnL'] += pnl_pct
                        
                        last_exit_idx[stock] = idx
                        del open_positions[stock]
                        
                else:
                    if stock in last_exit_idx and (idx - last_exit_idx[stock]) < R['cooldown_days']: continue
                    if strat_func(df, idx):
                        open_positions[stock] = {
                            'Entry': row['Close'],
                            'Target': row['Close'] * (1 + R['fixed_target_pct']/100),
                            'SL': row['Close'] * (1 - R['fixed_sl_pct']/100),
                            'Entry_Idx': idx
                        }

# Print the final execution scoreboard
print("="*75)
print("🏆 GRAND COMPARATIVE SCOREBOARD (ALL WATCHLIST SHARES - 1 YEAR) 🏆")
print("="*75)
report_data = []
for strat, metrics in perf_summary.items():
    tot = metrics['Total']
    wr = round((metrics['Wins'] / tot * 100), 1) if tot > 0 else 0.0
    report_data.append({
        'Strategy Name': strat,
        'Total Trades': tot,
        'Wins (Targets)': metrics['Wins'],
        'Losses/Time-Outs': metrics['Losses'],
        'Net Win Rate %': f"{wr}%",
        'Avg PnL per Trade': f"{round(metrics['PnL']/tot, 2)}%" if tot > 0 else "0%"
    })

df_report = pd.DataFrame(report_data)
print(df_report.to_string(index=False))
print("="*75)
