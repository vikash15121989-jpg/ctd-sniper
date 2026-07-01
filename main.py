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

print("=== V100.1: BULLETPROOF WATCHLIST SNIPER - FIXED ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== CONFIG =====
END_DATE = datetime.now().date()
START_DATE = END_DATE - timedelta(days=365)

MIN_AVG_VOLUME = 100000
MIN_AVG_TURNOVER_CR = 5
MIN_DATA_DAYS = 50 # FIX 1: Min 50 din ka data chahiye

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
ws_dhamaka_watch = get_or_create_sheet("Pre_Dhamaka_Watch")

print("All Sheets Connected Safely.", flush=True)

def get_watchlist_stocks():
    stocks = ws_watchlist.col_values(1)
    stocks = [s.strip().upper() for s in stocks if s.strip() and s.strip().upper() not in ['STOCK', 'SYMBOL', 'NAME']]
    stocks = [s + '.NS' if not s.endswith('.NS') and not s.startswith('^') else s for s in stocks]
    return stocks

# FIX 2: Robust column handling + NaN drop
def flatten_yf_columns(df):
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # Normalize column names
    df.columns = [str(col).strip() for col in df.columns]
    col_map = {col: col.capitalize() for col in df.columns}
    df.rename(columns=col_map, inplace=True)

    # Ensure Close exists
    if 'Close' not in df.columns:
        if 'Adj close' in df.columns:
            df['Close'] = df['Adj close']
        elif 'Adj Close' in df.columns:
            df['Close'] = df['Adj Close']

    # Drop rows where OHLCV is NaN
    df.dropna(subset=['Open', 'High', 'Low', 'Close', 'Volume'], inplace=True)
    return df

# ===== 🎯 THE ULTIMATE VIKASH CORE CONCEPT ENGINE (V100.1) 🎯 =====
def scan_pre_dhamaka(df, idx):
    """
    V100.1: BULLETPROOF + BUG-FREE
    Core Logic:
    1. Multi-Support (Min 2x in 1.8% Zone)
    2. Smart Money Footprint (2.5x Volume Climax in last 25 days)
    3. Look-Back Window (Squeeze/Dry-up pichle 4 dino me)
    4. Safe CMP Position (Floor ke paas)
    """
    # FIX 1: Minimum data check
    if idx < MIN_DATA_DAYS:
        return None

    # FIX: Skip if no volume today - halted stock
    if df.iloc[idx]['Volume'] == 0:
        return None

    current_close = df.iloc[idx]['Close']
    current_high = df.iloc[idx]['High']

    # 1. BADA FRAMEWORK (Pichle 100 dino ka data - excluding today)
    historical_100d = df.iloc[max(0, idx-100):idx]
    if len(historical_100d) < 20: # Safety
        return None

    absolute_low = historical_100d['Low'].min()

    # --- STEP 1: MULTI-SUPPORT CHECK ---
    support_zone_upper = absolute_low * 1.018
    candles_in_zone = historical_100d[historical_100d['Low'] <= support_zone_upper]
    total_touchpoints = len(candles_in_zone)

    # --- STEP 2: SMART MONEY ENTRY CHECK ---
    # FIX 4: Look-back only till idx-1, not idx
    vol_20ma_hist = df['Volume'].iloc[max(0, idx-35):idx].mean()
    if pd.isna(vol_20ma_hist) or vol_20ma_hist == 0:
        return None

    recent_25d = df.iloc[max(0, idx-25):idx]
    has_smart_money = (recent_25d['Volume'] > (vol_20ma_hist * 2.5)).any()

    # --- STEP 3 & 4: LOOK-BACK WINDOW FOR CONTRACTION & DRY-UP ---
    matched_in_window = False
    best_range_found = 100.0
    days_ago_matched = 0

    for shift in range(4): # 0=Aaj, 1=Kal, 2=Parso, 3=3 din pehle
        check_idx = idx - shift
        if check_idx < 20:
            continue

        # 6-day price contraction
        recent_6d = df['Close'].iloc[max(0, check_idx-5):check_idx+1] # 6 din = 0-5
        if len(recent_6d) < 6:
            continue
        price_range_pct = ((recent_6d.max() - recent_6d.min()) / recent_6d.min()) * 100

        # FIX 4: Volume avg me current din include nahi karna
        vol_20ma_current = df['Volume'].iloc[max(0, check_idx-20):check_idx].mean()
        if pd.isna(vol_20ma_current) or vol_20ma_current == 0:
            continue

        day_volume = df.iloc[check_idx]['Volume']

        # Squeeze + Volume Dry-Up dono sath?
        if price_range_pct <= 3.8 and day_volume < (vol_20ma_current * 0.85):
            matched_in_window = True
            if price_range_pct < best_range_found:
                best_range_found = price_range_pct
                days_ago_matched = shift

    # --- STEP 5: CURRENT PRICE POSITION ---
    distance_from_floor = ((current_close - absolute_low) / absolute_low) * 100
    is_at_support_now = 0.0 <= distance_from_floor <= 3.0

    # ===== TRIGGER COINCIDENCE LOGIC =====
    if total_touchpoints >= 2 and has_smart_money and matched_in_window and is_at_support_now:

        # FIX 6: Better SL using ATR
        atr = (df['High'] - df['Low']).iloc[max(0, idx-14):idx].mean()
        sl_by_floor = absolute_low * 0.99
        sl_by_atr = current_close - 1.5 * atr
        stop_loss = round(max(sl_by_floor, sl_by_atr, current_close * 0.95), 2) # Max 5% SL

        # FIX 3: Better Target calculation
        swing_high = df['High'].iloc[max(0, idx-100):idx].max()
        if swing_high <= current_close * 1.02: # Agar resistance paas hai
            target_1 = round(current_close * 1.10, 2) # 10% default
        else:
            target_1 = round(swing_high, 2)

        risk = current_close - stop_loss
        reward = target_1 - current_close

        # Risk to Reward + Liquidity check
        if risk > 0 and (reward / risk) >= 2.0 and current_close > stop_loss:
            status_msg = "Squeeze Today" if days_ago_matched == 0 else f"Squeeze {days_ago_matched}D Ago"
            return {
                'Stock': '', # Loop me fill hoga
                'Current_Close': round(current_close, 2),
                'Buy_Level': round(current_close, 2),
                'StopLoss': stop_loss,
                'Target': target_1,
                'RR': round(reward/risk, 1),
                'Details': f"Tested:{total_touchpoints}x | {status_msg} ({round(best_range_found, 1)}%)"
            }

    return None

# FIX 5: Safe sheet upload
def upload_to_sheet(ws, data_list, columns_order=None, default_msg="No Data"):
    try:
        ws.batch_clear(['A:Z']) # Safe clear
        time.sleep(1) # Rate limit bachao

        if data_list:
            df = pd.DataFrame(data_list)
            if columns_order:
                # Jo column nahi hai use add kar do
                for col in columns_order:
                    if col not in df.columns:
                        df[col] = ''
                df = df[columns_order]

            df_json = json.loads(df.to_json(orient='split'))
            values = [df_json['columns']] + df_json['data']
            ws.update(values=values, range_name='A1')
            print(f"Uploaded {len(data_list)} rows to {ws.title}", flush=True)
        else:
            ws.update(values=[[default_msg]], range_name='A1')
    except Exception as e:
        print(f"Sheet Upload Error: {str(e)}", flush=True)

# ===== MAIN ENGINE EXECUTION =====
stocks = get_watchlist_stocks()
final_dhamaka_watchlist = []

print(f"\n=== PROCESSING {len(stocks)} STOCKS FROM WATCHLIST ===", flush=True)

for i, stock in enumerate(stocks):
    try:
        print(f"[{i+1}/{len(stocks)}] Scanning {stock}...", end=' ', flush=True)

        stock_df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=False)
        stock_df = flatten_yf_columns(stock_df)

        if stock_df.empty or len(stock_df) < MIN_DATA_DAYS:
            print("Skipped: Insufficient data", flush=True)
            continue

        # Liquidity filters
        stock_df['Avg_Vol'] = stock_df['Volume'].rolling(window=20).mean()
        stock_df['Avg_Turnover'] = (stock_df['Close'] * stock_df['Volume']).rolling(window=20).mean() / 10000000

        curr_idx = len(stock_df) - 1
        avg_vol = stock_df.iloc[curr_idx]['Avg_Vol']
        avg_turnover = stock_df.iloc[curr_idx]['Avg_Turnover']

        if pd.isna(avg_vol) or pd.isna(avg_turnover):
            print("Skipped: NaN in filters", flush=True)
            continue

        if avg_vol < MIN_AVG_VOLUME or avg_turnover < MIN_AVG_TURNOVER_CR:
            print(f"Skipped: Low liquidity Vol:{int(avg_vol)} Cr:{round(avg_turnover,1)}", flush=True)
            continue

        setup = scan_pre_dhamaka(stock_df, curr_idx)
        if setup:
            setup['Stock'] = stock.replace('.NS', '')
            final_dhamaka_watchlist.append(setup)
            print(f"🔥 RADAR MATCHED! {setup['Details']} | RR:{setup['RR']}", flush=True)
        else:
            print("No setup", flush=True)

        time.sleep(0.15) # YF rate limit se bachne ke liye
    except Exception as e:
        print(f"Error - {str(e)}", flush=True)

# Export to Google Sheet
columns = ['Stock', 'Current_Close', 'Buy_Level', 'StopLoss', 'Target', 'RR', 'Details']
upload_to_sheet(ws_dhamaka_watch, final_dhamaka_watchlist, columns, "No Pre-Dhamaka Setup Found Today")
print("\n=== GOOGLE SHEET UPDATED SUCCESSFULLY ===", flush=True)
print(f"Total Matches: {len(final_dhamaka_watchlist)}", flush=True)
