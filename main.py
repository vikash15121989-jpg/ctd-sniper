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

print("=== RS BEATER V25 - CUSTOM PA NO-GIMMICK BACKTESTER ===", flush=True)
print(f"Backtest Period: {BACKTEST_START} to {BACKTEST_END}", flush=True)

gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

R = {
    'min_daily_value_cr': 30.0,    
    'fixed_target_pct': 6.0,       # Targeted 6% setup
    'fixed_sl_pct': 3.0,           # Strict 3% risk management
    'time_stop_days': 10,          # 10 Days time stop for complex PA
    'risk_per_trade': 10000,       
    'cooldown_days': 5,            
    'max_open_trades': 6,          
}

def get_or_create_ws(sh, title):
    try: return sh.worksheet(title)
    except: return sh.add_worksheet(title=title, rows=10000, cols=30)

def calculate_custom_indicators(df):
    # RSI for Hidden Accumulation
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    # Track historical lows for Stop-Hunt detection
    df['Low_Min_10D'] = df['Low'].shift(1).rolling(window=10).min()
    df['Close_Min_10D'] = df['Close'].shift(1).rolling(window=10).min()
    
    return df

def check_custom_smart_money_pattern(df, idx):
    if idx < 20: return False
    
    row_today = df.iloc[idx]
    row_prev = df.iloc[idx-1]
    row_prev2 = df.iloc[idx-2]
    
    # 1. STOP HUNT CHECK: Low ne 10-day low ko toda, lekin Close uske upar aa gayi (Spring/Wick Rejection)
    stop_hunt = (row_today['Low'] < row_today['Low_Min_10D']) and (row_today['Close'] > row_today['Low_Min_10D'])
    
    # 2. HIDDEN ACCUMULATION CHECK: Price flat/niche hai par RSI pichle dino se strong hai
    price_falling_or_flat = df['Close'].iloc[idx] <= df['Close'].iloc[idx-10]
    rsi_rising = df['RSI'].iloc[idx] > df['RSI'].iloc[idx-10]
    hidden_accumulation = price_falling_or_flat and rsi_rising
    
    # 3. FAIR VALUE GAP (LIQUIDITY VOID) CHECK: Imbalance in last 3 candles
    # Today's Low is greater than 2-days-ago High -> Valid Bullish FVG
    fvg_detected = row_today['Low'] > row_prev2['High']
    
    # Aggrigate Trigger: Agar Teeno me se kam se kam any TWO features ya STOP HUNT strictly valid ho
    if stop_hunt or (hidden_accumulation and fvg_detected):
        # Candle wick must show rejection
        candle_range = row_today['High'] - row_today['Low']
        if candle_range > 0:
            lower_wick = min(row_today['Open'], row_today['Close']) - row_today['Low']
            if (lower_wick / candle_range) >= 0.35: # Strong lower tail
                return True
                
    return False

def download_single_stock(stock):
    try:
        ticker = stock if stock.endswith('.NS') else f"{stock}.NS"
        df = yf.download(ticker, start=BACKTEST_START - timedelta(days=100),
                       end=BACKTEST_END + timedelta(days=1), progress=False, auto_adjust=True)
        if df.empty or len(df) < 50: return None, stock
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        df = calculate_custom_indicators(df)
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
last_exit_dates = {}
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

            # Trailing logic at 3% to lock risk
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
            if i < 20: continue
            row = df.iloc[i]

            avg_value_cr = (df['Close'].iloc[max(0,i-20):i] * df['Volume'].iloc[max(0,i-20):i]).mean() / 1e7
            if pd.isna(avg_value_cr) or avg_value_cr < R['min_daily_value_cr']: continue

            # RUNNING CUSTOM NO-GIMMICK ENGINE
            if not check_custom_smart_money_pattern(df, i): continue

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

# CUSTOM PRO LEADEDBOARD ANALYSIS
df_perf = pd.DataFrame.from_dict(stock_perf, orient='index').reset_index().rename(columns={'index': 'Stock'})
if not df_perf.empty:
    df_perf['Win_Rate_%'] = round((df_perf['Wins'] / df_perf['Total_Trades']) * 100, 1)
    df_perf = df_perf[df_perf['Total_Trades'] >= 2]  # Minimum 2 patterns verified
    df_perf = df_perf.sort_values(by=['Wins', 'Win_Rate_%'], ascending=[False, False])

print("\n" + "="*60)
print("🏆 CUSTOM HOLY GRAIL LEADERBOARD (SMART MONEY TRAP) 🏆")
print("="*60)
if df_perf.empty:
    print("Is custom configuration par koi sample generate nahi hua. Let's adjust weights if empty.")
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
