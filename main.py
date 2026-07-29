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

print("=== V112.3: EOD INSIDE-BOX PURE VOL-ACCUMULATION ENGINE (NEXT-DAY TRIGGER) ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== CONFIG =====
END_DATE = (datetime.now() + timedelta(days=1)).date()
START_DATE = END_DATE - timedelta(days=365)

MIN_AVG_VOLUME = 100000
MIN_AVG_TURNOVER_CR = 2

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
ws_ready_today = get_or_create_sheet("Ready_For_Today")  # Agle din ki entry ke liye sheet

def get_watchlist_stocks():
    stocks = ws_watchlist.col_values(1)
    stocks = [s.strip().upper() for s in stocks if s.strip() and s.strip().upper() not in ['STOCK', 'SYMBOL', 'NAME']]
    stocks = [s + '.NS' if not s.endswith('.NS') and not s.startswith('^') else s for s in stocks]
    return stocks

def flatten_yf_columns(df):
    if df.empty: return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(col).strip() for col in df.columns]
    col_map = {col: col.capitalize() for col in df.columns}
    df.rename(columns=col_map, inplace=True)
    if 'Close' not in df.columns:
        if 'Adj close' in df.columns: df['Close'] = df['Adj close']
        elif 'Adj Close' in df.columns: df['Close'] = df['Adj Close']
    df.dropna(subset=['Open', 'High', 'Low', 'Close', 'Volume'], inplace=True)
    return df

# ===== 🎯 PURE VOL-ACCUMULATION EOD SCANNER 🎯 =====
def scan_pure_vol_dry_squeeze(df):
    total_rows = len(df)
    if total_rows < 60: return None

    # EOD Scanning: Last row is today's completed candle
    eod_idx = total_rows - 1
    eod_close = df.iloc[eod_idx]['Close']
    eod_open = df.iloc[eod_idx]['Open']
    eod_high = df.iloc[eod_idx]['High']
    eod_low = df.iloc[eod_idx]['Low']
    eod_vol = df.iloc[eod_idx]['Volume']
    prev_close = df.iloc[eod_idx - 1]['Close']
    
    # 20-Day Rolling Average Volume
    df['Vol_Avg_20'] = df['Volume'].rolling(window=20).mean()
    possible_anchors = []
    
    # 1. Past 40 days me Anchor Day dhoondhna (Aaj ki completed candle tak check)
    for idx in range(max(20, eod_idx - 40), eod_idx + 1):
        check_vol = df.iloc[idx]['Volume']
        check_close = df.iloc[idx]['Close']
        check_open = df.iloc[idx]['Open']
        avg_vol_then = df.iloc[idx-1]['Vol_Avg_20']
        
        if pd.isna(avg_vol_then) or avg_vol_then == 0: continue
        
        # Anchor Condition: 3x Volume + Green Candle
        if check_vol > (avg_vol_then * 3.0) and check_close > check_open:
            possible_anchors.append({
                'idx': idx,
                'date': df.index[idx].strftime('%d-%b')
            })
            
    if not possible_anchors:
        return None
        
    best_anchor = possible_anchors[-1] 
    anchor_row_idx = best_anchor['idx']
    anchor_date = best_anchor['date']
    
    # Pre-Anchor Support Level (Swing Low before Anchor)
    pre_anchor_zone = df.iloc[max(0, anchor_row_idx-20):anchor_row_idx]
    if not pre_anchor_zone.empty:
        pre_anchor_support = pre_anchor_zone['Low'].min()
    else:
        pre_anchor_support = df.iloc[anchor_row_idx]['Low']
        
    # Consolidation Box High (Breakout / Trigger Level for Tomorrow)
    post_anchor_zone = df.iloc[anchor_row_idx:eod_idx+1]
    box_high = post_anchor_zone['High'].max()
        
    is_support_safe = True
    dry_up_days = 0
    total_base_days = max(1, eod_idx - anchor_row_idx)
    
    # Support Safety & Volume Dry Check (Anchor se lekar EOD tak)
    for check_idx in range(anchor_row_idx + 1, total_rows):
        f_close = df.iloc[check_idx]['Close']
        f_vol = df.iloc[check_idx]['Volume']
        avg_vol_that_day = df.iloc[check_idx]['Vol_Avg_20']
        
        # 1% Buffer for Support Safety (Wick Spike Protection)
        if f_close < (pre_anchor_support * 0.99):
            is_support_safe = False
            break
        
        if not pd.isna(avg_vol_that_day) and f_vol < avg_vol_that_day:
            dry_up_days += 1
            
    if is_support_safe:
        dry_ratio = dry_up_days / total_base_days if total_base_days > 0 else 0
        grade = "A+" if dry_ratio >= 0.70 else ("A" if dry_ratio >= 0.45 else "B")
        
        # ===== 🎯 READY FOR TOMORROW LOGIC 🎯 =====
        # 1. Close is sitting near Box High (Within 3% of Breakout Level)
        near_breakout = (eod_close >= box_high * 0.97)
        
        # 2. Strong Closing (Close in top 30% of day's High-Low range)
        day_range = eod_high - eod_low
        strong_close = (eod_close >= (eod_high - day_range * 0.30)) if day_range > 0 else True
        
        # 3. Positive Day Close (Green Candle & Above Prev Close)
        is_price_up = (eod_close > prev_close) and (eod_close >= eod_open)
        
        # Agle din ke liye tabhi Select hoga jab Trigger Level ke paas strong close ho
        is_ready_tomorrow = near_breakout and strong_close and is_price_up
            
        stop_loss = round(pre_anchor_support, 2)
        target_1 = round(eod_close * 1.15, 2)
        risk = max(0.01, eod_close - stop_loss)
        reward = target_1 - eod_close

        return {
            'Stock': '', 
            'Grade': grade,
            'Current_Close': round(eod_close, 2),
            'Trigger_Above': round(box_high, 2),
            'Pre_Anchor_SL': stop_loss,
            'Target': target_1,
            'RR': round(reward/risk, 1),
            'Details': f"Anchor:[{anchor_date}] | Pre-Support:{round(pre_anchor_support,1)} | DryDays:{dry_up_days}/{total_base_days}",
            'Ready_Tomorrow': is_ready_tomorrow
        }
        
    return None

def upload_to_sheet(ws, data_list, columns_order=None, default_msg="No Data"):
    try:
        ws.batch_clear(['A:Z'])
        time.sleep(1)
        if data_list:
            df = pd.DataFrame(data_list)
            if columns_order:
                for col in columns_order:
                    if col not in df.columns: df[col] = ''
                df = df[columns_order]
            if 'Grade' in df.columns:
                df['Grade_Score'] = df['Grade'].map({'A+': 3, 'A': 2, 'B': 1})
                df = df.sort_values(by='Grade_Score', ascending=False).drop(columns=['Grade_Score'])
                
            df_json = json.loads(df.to_json(orient='split'))
            values = [df_json['columns']] + df_json['data']
            ws.update(values=values, range_name='A1')
        else:
            ws.update(values=[[default_msg]], range_name='A1')
    except Exception as e:
        print(f"Sheet Error: {str(e)}", flush=True)

# ===== MAIN EXECUTION LOOP =====
stocks = get_watchlist_stocks()
pre_dhamaka_watchlist = []
ready_today_watchlist = [] 

REJECT_KEYWORDS = ['LIQUID', 'ETF', 'CPSE', 'NETF', 'GILT', 'GOLD', 'SILVER']

print(f"\n=== SCANNING {len(stocks)} STOCKS FOR EOD VOL-BLAST (NEXT-DAY SELECTION) ===", flush=True)

for i, stock in enumerate(stocks):
    try:
        symbol_clean = stock.replace('.NS', '')
        if any(keyword in symbol_clean for keyword in REJECT_KEYWORDS): continue

        stock_df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=True)
        stock_df = flatten_yf_columns(stock_df)

        if stock_df.empty or len(stock_df) < 60: continue

        stock_df['Avg_Vol'] = stock_df['Volume'].rolling(window=20).mean()
        stock_df['Avg_Turnover'] = (stock_df['Close'] * stock_df['Volume']).rolling(window=20).mean() / 10000000

        curr_idx = len(stock_df) - 1
        avg_vol = stock_df.iloc[curr_idx]['Avg_Vol']
        avg_turnover = stock_df.iloc[curr_idx]['Avg_Turnover']

        if pd.isna(avg_vol) or pd.isna(avg_turnover) or avg_vol < MIN_AVG_VOLUME or avg_turnover < MIN_AVG_TURNOVER_CR:
            continue

        setup = scan_pure_vol_dry_squeeze(stock_df)
        if setup:
            setup['Stock'] = symbol_clean
            
            # Agar stock agle din trigger level cross karne ke bilkul paas hai
            if setup['Ready_Tomorrow']:
                clean_setup = setup.copy()
                clean_setup.pop('Ready_Tomorrow', None)
                ready_today_watchlist.append(clean_setup)
            
            setup.pop('Ready_Tomorrow', None)
            pre_dhamaka_watchlist.append(setup)

        time.sleep(0.15)
    except Exception as e:
        pass

columns = ['Stock', 'Grade', 'Current_Close', 'Trigger_Above', 'Pre_Anchor_SL', 'Target', 'RR', 'Details']
upload_to_sheet(ws_dhamaka_watch, pre_dhamaka_watchlist, columns, "No Vol-Dry Squeeze Stock Found Today")
upload_to_sheet(ws_ready_today, ready_today_watchlist, columns, "Agle din entry ke liye koi Box High Breakout ke paas stock nahi mila.")

print("\n=== EOD SYSTEM EXECUTION COMPLETED SUCCESSFULLY ===", flush=True)
