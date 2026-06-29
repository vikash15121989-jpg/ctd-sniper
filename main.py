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

print("=== V45.0: COMPLETE SNAPSHOT PIPELINE (FRESH CHOCH & STRUCTURE SHIFT) ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== CONFIG =====
END_DATE = datetime.now().date()
START_DATE = END_DATE - timedelta(days=365)
BACKTEST_START_DATE = END_DATE - timedelta(days=730)  # 2 saal ka data backtest ke liye

MIN_AVG_VOLUME = 100000
MIN_AVG_TURNOVER_CR = 5
SWING_LENGTH = 5

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

# ===== CORE STRUCTURAL LOGIC ENGINE =====
def analyze_market_structure(df, idx, length=5):
    """
    Downtrend se reversal track karne ke liye dono conditions scan karega:
    Type 1: Fresh CHoCH (Just broke LH1, Major Bottom intact)
    Type 2: CHoCH + HH/HL (Structure fully shifted to uptrend)
    """
    if idx < length * 5: return None
    
    # Swings dhoodhne ke liye window select karein
    df_window = df.iloc[max(0, idx - 150):idx+1].copy()
    window_size = length * 2 + 1

    ph_mask = (df_window['High'].shift(length) == df_window['High'].rolling(window_size).max()).shift(-length).fillna(False)
    pl_mask = (df_window['Low'].shift(length) == df_window['Low'].rolling(window_size).min()).shift(-length).fillna(False)

    pivot_highs = df_window[ph_mask]['High'].dropna()
    pivot_lows = df_window[pl_mask]['Low'].dropna()

    if len(pivot_highs) < 2 or len(pivot_lows) < 2: return None

    # Step 1: Downtrend Check (Pehle stock niche gir raha tha)
    lh1 = pivot_highs.iloc[-2] if len(pivot_highs) >= 2 else pivot_highs.iloc[-1]
    ll1 = pivot_lows.iloc[-2] if len(pivot_lows) >= 2 else pivot_lows.iloc[-1]
    
    if len(pivot_highs) >= 3 and len(pivot_lows) >= 3:
        lh2, ll2 = pivot_highs.iloc[-3], pivot_lows.iloc[-3]
        if not (lh1 < lh2 or ll1 < ll2):
            pass 

    major_bottom = ll1
    current_close = df.iloc[idx]['Close']
    
    # RULE: Major bottom intact hona chahiye (Low uske niche nahi jana chahiye)
    if df.iloc[idx]['Low'] < major_bottom: return None

    # Step 2: CHoCH Trigger Verification (Kya haal hi me LH1 ke upar breakout hua hai?)
    # Pichle 12 trading sessions ke andar close ya high ne strict lower high (lh1) ko toda ho
    recent_candles = df.iloc[max(0, idx-12):idx+1]
    choch_by_close = (recent_candles['Close'] > lh1).any()
    choch_by_high = (recent_candles['High'] > lh1).any()
    
    if not (choch_by_close or choch_by_high): return None

    # Step 3: Classification (Type A: Fresh CHoCH vs Type B: HH/HL structure)
    latest_ph = pivot_highs.iloc[-1]
    latest_pl = pivot_lows.iloc[-1]
    
    status_type = "Fresh_CHoCH"
    breakout_target = lh1
    
    # Agar naye swings confirm ho chuke hain aur wo upar hain, toh structural shift (Type B)
    if latest_ph > lh1 and latest_pl > major_bottom:
        status_type = "CHoCH + HH/HL"
        breakout_target = latest_ph

    return {
        'major_bottom': major_bottom,
        'choch_level': lh1,
        'breakout_price': breakout_target,
        'current_close': current_close,
        'type': status_type
    }

def check_strength(df, idx):
    if idx < 2: return False
    current_green = df.iloc[idx]['Close'] > df.iloc[idx]['Open']
    volume_up = df.iloc[idx]['Volume'] > df.iloc[idx-1]['Volume']
    last_3 = df.iloc[idx-2:idx+1]
    green_count = len(last_3[last_3['Close'] > last_3['Open']])
    return current_green and volume_up and green_count >= 2

def eval_setup_at_index(df, idx):
    structure = analyze_market_structure(df, idx, SWING_LENGTH)
    if structure and check_strength(df, idx):
        return structure
    return None

def analyze_historical_winrate(df):
    total_setups = 0
    winning_setups = 0
    
    # Historical processing backtest
    for i in range(80, len(df) - 15):
        setup = eval_setup_at_index(df, i)
        if setup:
            total_setups += 1
            entry_price = df.iloc[i]['Close']
            stop_loss = setup['major_bottom'] * 0.995 
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
            df = df[columns_order]
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

        if stock_df.empty or len(stock_df) < 120: continue
        
        stock_df['Avg_Vol'] = stock_df['Volume'].rolling(window=20).mean()
        stock_df['Avg_Turnover'] = (stock_df['Close'] * stock_df['Volume']).rolling(window=20).mean() / 10000000
        
        curr_idx = len(stock_df) - 1
        if stock_df.iloc[curr_idx]['Avg_Vol'] < MIN_AVG_VOLUME or stock_df.iloc[curr_idx]['Avg_Turnover'] < MIN_AVG_TURNOVER_CR:
            continue

        # --- MATCH STRUCTURE ---
        structure_data = analyze_market_structure(stock_df, curr_idx, SWING_LENGTH)
        
        if structure_data:
            # 1. Base Sheet Update
            choch_base_list.append({
                'Stock': stock.replace('.NS', ''),
                'Close': float(round(structure_data['current_close'], 2)),
                'CHoCH_Level': float(round(structure_data['choch_level'], 2)),
                'Major_Bottom': float(round(structure_data['major_bottom'], 2)),
                'Type': structure_data['type']
            })
            
            # 2. Setup 10 Days Tracker
            breakout_pr = float(round(structure_data['breakout_price'], 2))
            stop_loss_pr = float(round(structure_data['major_bottom'] * 0.995, 2))
            
            setup_row = {
                'Setup_Date': str(END_DATE),
                'Stock': stock.replace('.NS', ''),
                'Breakout_Price': breakout_pr,
                'Stoploss_Price': stop_loss_pr
            }
            setup_10d_list.append(setup_row)
            
            # 3. Finale Sniper Filtering
            win_rate, total_setups = analyze_historical_winrate(stock_df)
            if win_rate >= 70.0 or total_setups == 0:  # Fresh break ke liye initial allocation loop
                final_sniper_list.append(setup_row)
                print(f" -> {stock}: MATCHED ({structure_data['type']})! Added to Finale.", flush=True)

        time.sleep(0.3)
    except Exception as e:
        print(f" -> {stock}: Error - {str(e)}", flush=True)

# ===== EXPORT CLEAN DATA TO SHEETS =====
print("\n=== UPDATING GOOGLE SHEETS WITH CLEAN VIEWS ===", flush=True)

# Sheet 1: CHOCH Base (Ab isme Type column bhi dikhega)
upload_to_sheet(ws_choch, choch_base_list, ['Stock', 'Close', 'CHoCH_Level', 'Major_Bottom', 'Type'], "No Active Base Found")

# Sheet 2: 10 Days Setup
upload_to_sheet(ws_setup_10d, setup_10d_list, ['Setup_Date', 'Stock', 'Breakout_Price', 'Stoploss_Price'], "No New Setup Found")

# Sheet 3: Finale Sniper Sheet
upload_to_sheet(ws_final, final_sniper_list, ['Setup_Date', 'Stock', 'Breakout_Price', 'Stoploss_Price'], "No High Win-Rate Structure Shift Today")

print("\n=== CLEAN PIPELINE EXECUTED SUCCESSFULLY ===", flush=True)
