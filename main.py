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

print("=== V43.0: CLEAN FINALE SNIPER PIPELINE ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== CONFIG =====
END_DATE = datetime.now().date()
START_DATE = END_DATE - timedelta(days=365)
BACKTEST_START_DATE = END_DATE - timedelta(days=730)  # 2 saal ka data backtest ke liye

MIN_AVG_VOLUME = 100000
MIN_AVG_TURNOVER_CR = 5
SWING_LENGTH = 5
PULLBACK_ZONE_PCT = 3.0
BREAKOUT_BUFFER_PCT = 5.0 

# ===== GOOGLE SHEETS SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")

def get_or_create_sheet(title):
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows="1000", cols="20")

ws_watchlist = sh.worksheet("Watchlist")
ws_choch = get_or_create_sheet("CHOCH_Base")
ws_setup_10d = get_or_create_sheet("Setup_10Days")
ws_final = get_or_create_sheet("Final_Sniper_90Pct")

print("All Sheets Connected Safely.", flush=True)

def get_watchlist_stocks():
    stocks = ws_watchlist.col_values(1)
    stocks = [s.strip().upper() for s in stocks if s.strip() and s.strip().upper() not in ['STOCK', 'SYMBOL', 'NAME']]
    stocks = [s + '.NS' if not s.endswith('.NS') and not s.startswith('^') else s for s in stocks]
    return stocks

def flatten_yf_columns(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(col).strip().title() for col in df.columns]
    if 'Adj Close' in df.columns and 'Close' not in df.columns:
        df['Close'] = df['Adj Close']
    return df

def get_swing_levels(df, idx, length=5):
    if idx < length * 2: return None
    df_window = df.iloc[max(0, idx - 150):idx+1].copy()
    window_size = length * 2 + 1

    ph_mask = (df_window['High'].shift(length) == df_window['High'].rolling(window_size).max()).shift(-length).fillna(False)
    pl_mask = (df_window['Low'].shift(length) == df_window['Low'].rolling(window_size).min()).shift(-length).fillna(False)

    pivot_highs = df_window[ph_mask]['High'].dropna().tail(2)
    pivot_lows = df_window[pl_mask]['Low'].dropna().tail(2)

    if len(pivot_highs) < 2 or len(pivot_lows) < 2: return None
    return {
        'latest_ph': pivot_highs.iloc[-1], 'prev_ph': pivot_highs.iloc[-2],
        'latest_pl': pivot_lows.iloc[-1], 'prev_pl': pivot_lows.iloc[-2]
    }

def check_choch_major_bottom(df, idx, lookback=60):
    if idx < lookback: return False, None
    window = df.iloc[idx-lookback:idx+1]
    major_bottom_idx = window['Low'].idxmin()
    major_bottom_price = window.loc[major_bottom_idx, 'Low']
    major_bottom_loc = df.index.get_loc(major_bottom_idx)

    if df.iloc[idx]['Close'] < major_bottom_price: return False, None
    post_bottom_df = df.iloc[major_bottom_loc:idx+1]
    if post_bottom_df['Low'].min() < major_bottom_price * 0.99: return False, None
    return True, major_bottom_price

def check_hh_hl_swing_structure(swing_data, current_close):
    if swing_data is None: return False
    return swing_data['latest_ph'] > swing_data['prev_ph'] and swing_data['latest_pl'] > swing_data['prev_pl'] and current_close > swing_data['latest_pl']

def check_pullback_to_swing_support(df, idx, swing_data):
    if swing_data is None: return False, None, None
    current_close = df.iloc[idx]['Close']
    if current_close > swing_data['latest_ph'] * (1 + BREAKOUT_BUFFER_PCT/100): return False, None, None

    for target, name in [(swing_data['prev_ph'], "Prev_Swing_High"), (swing_data['prev_pl'], "Prev_Swing_Low")]:
        if abs((current_close - target) / target) * 100 <= PULLBACK_ZONE_PCT:
            return True, name, target
    return False, None, None

def check_strength(df, idx):
    if idx < 2: return False
    current_green = df.iloc[idx]['Close'] > df.iloc[idx]['Open']
    volume_up = df.iloc[idx]['Volume'] > df.iloc[idx-1]['Volume']
    last_3 = df.iloc[idx-2:idx+1]
    green_count = len(last_3[last_3['Close'] > last_3['Open']])
    return current_green and volume_up and green_count >= 2

def eval_setup_at_index(df, idx):
    swing_data = get_swing_levels(df, idx, SWING_LENGTH)
    if not swing_data or not check_hh_hl_swing_structure(swing_data, df.iloc[idx]['Close']): return None
    pullback_ok, p_to, supp = check_pullback_to_swing_support(df, idx, swing_data)
    if pullback_ok and check_strength(df, idx):
        return {'p_to': p_to, 'supp': supp, 'swing': swing_data}
    return None

def analyze_historical_winrate(df):
    total_setups = 0
    winning_setups = 0
    
    for i in range(60, len(df) - 5):
        setup = eval_setup_at_index(df, i)
        if setup:
            total_setups += 1
            entry_price = df.iloc[i]['Close']
            stop_loss = setup['supp'] * 0.99
            target_price = entry_price + (entry_price - stop_loss) * 1.5
            
            for j in range(i+1, min(i+16, len(df))):
                if df.iloc[j]['Low'] <= stop_loss:
                    break
                if df.iloc[j]['High'] >= target_price:
                    winning_setups += 1
                    break
                    
    win_rate = (winning_setups / total_setups * 100) if total_setups > 0 else 0
    return win_rate, total_setups

def upload_to_sheet(ws, data_list, columns_order=None, default_msg="No Data"):
    ws.clear()
    time.sleep(0.5)
    if data_list:
        df = pd.DataFrame(data_list)
        if columns_order:
            df = df[columns_order]  # Strict Clean Column Filtering
        df_json = json.loads(df.to_json(orient='split'))
        values = [df_json['columns']] + df_json['data']
        ws.update(values=values, range_name='A1')
    else:
        ws.update(values=[[default_msg]], range_name='A1')

# ===== MAIN ENGINE =====
stocks = get_watchlist_stocks()
choch_base_list = []
setup_10d_list = []
final_sniper_list = []

print(f"\n=== PROCESSING {len(stocks)} STOCKS ===", flush=True)

for i, stock in enumerate(stocks):
    try:
        print(f"[{i+1}/{len(stocks)}] Analyzing {stock}...", flush=True)
        stock_df = yf.download(stock, start=BACKTEST_START_DATE, end=END_DATE, progress=False, auto_adjust=False)
        stock_df = flatten_yf_columns(stock_df)

        if stock_df.empty or len(stock_df) < 100: continue
        
        stock_df['Avg_Vol'] = stock_df['Volume'].rolling(window=20).mean()
        stock_df['Avg_Turnover'] = (stock_df['Close'] * stock_df['Volume']).rolling(window=20).mean() / 10000000
        
        curr_idx = len(stock_df) - 1
        if stock_df.iloc[curr_idx]['Avg_Vol'] < MIN_AVG_VOLUME or stock_df.iloc[curr_idx]['Avg_Turnover'] < MIN_AVG_TURNOVER_CR:
            continue

        # --- STAGE 1: CHOCH CHECK ---
        choch_ok, major_bottom = check_choch_major_bottom(stock_df, curr_idx)
        if choch_ok:
            choch_base_list.append({
                'Stock': stock.replace('.NS', ''),
                'Close': float(round(stock_df.iloc[curr_idx]['Close'], 2)),
                'Major_Bottom': float(round(major_bottom, 2))
            })
            
            # --- STAGE 2: 10 DAYS SETUP TRACKER ---
            found_in_10d = False
            setup_date = None
            setup_details = None
            
            for d in range(0, 10):
                target_idx = curr_idx - d
                if target_idx < 60: break
                
                setup = eval_setup_at_index(stock_df, target_idx)
                if setup:
                    found_in_10d = True
                    setup_date = stock_df.index[target_idx].date()
                    setup_details = setup
                    break
            
            if found_in_10d:
                breakout_pr = float(round(setup_details['swing']['latest_ph'] * 1.001, 2))
                stop_loss_pr = float(round(setup_details['supp'] * 0.99, 2))
                
                setup_row = {
                    'Setup_Date': str(setup_date),
                    'Stock': stock.replace('.NS', ''),
                    'Breakout_Price': breakout_pr,
                    'Stoploss_Price': stop_loss_pr
                }
                setup_10d_list.append(setup_row)
                
                # --- STAGE 3: THE FINALE METRICS (BACKEND FILTERS ONLY) ---
                win_rate, total_setups = analyze_historical_winrate(stock_df)
                
                if win_rate >= 90.0 and total_setups <= 3:
                    # Storing ONLY requested execution data
                    final_sniper_list.append({
                        'Setup_Date': str(setup_date),
                        'Stock': stock.replace('.NS', ''),
                        'Breakout_Price': breakout_pr,
                        'Stoploss_Price': stop_loss_pr
                    })
                    print(f" -> {stock}: MATCHED METRICS! Added to Finale.", flush=True)

        time.sleep(0.3)
    except Exception as e:
        print(f" -> {stock}: Error - {str(e)}", flush=True)

# ===== EXPORT CLEAN DATA TO SHEETS =====
print("\n=== UPDATING GOOGLE SHEETS WITH CLEAN VIEWS ===", flush=True)

# Sheet 1: CHOCH Base
upload_to_sheet(ws_choch, choch_base_list, default_msg="No Base Setup Found")

# Sheet 2: 10 Days Setup (Sorted by Date Descending)
if setup_10d_list:
    setup_10d_list = sorted(setup_10d_list, key=lambda x: x['Setup_Date'], reverse=True)
upload_to_sheet(ws_setup_10d, setup_10d_list, ['Setup_Date', 'Stock', 'Breakout_Price', 'Stoploss_Price'], "No Setups in last 10 Days")

# Sheet 3: Finale Sheet (CLEAN & NO HUTCH-POTCH)
if final_sniper_list:
    final_sniper_list = sorted(final_sniper_list, key=lambda x: x['Setup_Date'], reverse=True)
upload_to_sheet(ws_final, final_sniper_list, ['Setup_Date', 'Stock', 'Breakout_Price', 'Stoploss_Price'], "No 90%+ Win-Rate Sniper Setup Today")

print("\n=== CLEAN PIPELINE EXECUTED SUCCESSFULLY ===", flush=True)
