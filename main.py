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

print("=== V113.0: EOD INSIDE-BOX VOL-ACCUMULATION + SUPPORT BULLISH REVERSAL ENGINE ===", flush=True)
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
ws_ready_today = get_or_create_sheet("Ready_For_Today")
ws_uptrend_3d = get_or_create_sheet("UpTrend_3D_Dhamaka")
ws_support_bullish = get_or_create_sheet("Support_Bullish_Reversal")  # 🎯 Support + Bullish Candle Sheet

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

# ===== 🎯 CANDLESTICK PATTERN CHECKER 🎯 =====
def check_bullish_candle_pattern(df, curr_idx):
    c_open = df.iloc[curr_idx]['Open']
    c_close = df.iloc[curr_idx]['Close']
    c_high = df.iloc[curr_idx]['High']
    c_low = df.iloc[curr_idx]['Low']
    
    p_open = df.iloc[curr_idx - 1]['Open']
    p_close = df.iloc[curr_idx - 1]['Close']
    
    body = abs(c_close - c_open)
    total_range = c_high - c_low
    if total_range == 0: return False, ""

    upper_shadow = c_high - max(c_open, c_close)
    lower_shadow = min(c_open, c_close) - c_low

    # 1. Hammer (Green ya Red)
    is_hammer = (lower_shadow >= 2.0 * body) and (upper_shadow <= body * 0.5) and (body > 0)
    
    # 2. Bullish Engulfing
    p_body = abs(p_close - p_open)
    is_prev_red = p_close < p_open
    is_curr_green = c_close > c_open
    is_engulfing = is_prev_red and is_curr_green and (c_close >= p_open) and (c_open <= p_close)
    
    # 3. Pure Strong Bullish Candle
    is_pure_bullish = is_curr_green and (body / total_range >= 0.65) and (upper_shadow <= total_range * 0.15)

    if is_engulfing:
        return True, "Bullish Engulfing"
    elif is_hammer:
        return True, "Hammer (Green)" if is_curr_green else "Hammer (Red)"
    elif is_pure_bullish:
        return True, "Pure Bullish"
        
    return False, ""

# ===== 🎯 PURE VOL-ACCUMULATION & SCANNER ENGINE 🎯 =====
def scan_pure_vol_dry_squeeze(df):
    total_rows = len(df)
    if total_rows < 60: return None

    eod_idx = total_rows - 1
    eod_close = df.iloc[eod_idx]['Close']
    eod_open = df.iloc[eod_idx]['Open']
    eod_high = df.iloc[eod_idx]['High']
    eod_low = df.iloc[eod_idx]['Low']
    eod_vol = df.iloc[eod_idx]['Volume']
    prev_close = df.iloc[eod_idx - 1]['Close']
    
    df['Vol_Avg_20'] = df['Volume'].rolling(window=20).mean()
    possible_anchors = []
    
    for idx in range(max(20, eod_idx - 40), eod_idx + 1):
        check_vol = df.iloc[idx]['Volume']
        check_close = df.iloc[idx]['Close']
        check_open = df.iloc[idx]['Open']
        avg_vol_then = df.iloc[idx-1]['Vol_Avg_20']
        
        if pd.isna(avg_vol_then) or avg_vol_then == 0: continue
        
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
    
    pre_anchor_zone = df.iloc[max(0, anchor_row_idx-20):anchor_row_idx]
    pre_anchor_support = pre_anchor_zone['Low'].min() if not pre_anchor_zone.empty else df.iloc[anchor_row_idx]['Low']
        
    post_anchor_zone = df.iloc[anchor_row_idx:eod_idx+1]
    box_high = post_anchor_zone['High'].max()
        
    is_support_safe = True
    dry_up_days = 0
    total_base_days = max(1, eod_idx - anchor_row_idx)
    
    for check_idx in range(anchor_row_idx + 1, total_rows):
        f_close = df.iloc[check_idx]['Close']
        f_vol = df.iloc[check_idx]['Volume']
        avg_vol_that_day = df.iloc[check_idx]['Vol_Avg_20']
        
        if f_close < (pre_anchor_support * 0.99):
            is_support_safe = False
            break
        
        if not pd.isna(avg_vol_that_day) and f_vol < avg_vol_that_day:
            dry_up_days += 1
            
    if is_support_safe:
        dry_ratio = dry_up_days / total_base_days if total_base_days > 0 else 0
        grade = "A+" if dry_ratio >= 0.70 else ("A" if dry_ratio >= 0.50 else "B")
        avg_vol_20_today = df.iloc[eod_idx]['Vol_Avg_20']
        
        # 1. STRICT READY FOR TOMORROW LOGIC
        very_near_breakout = (eod_close >= box_high * 0.985)
        day_range = eod_high - eod_low
        strong_close = (eod_close >= (eod_high - day_range * 0.25)) if day_range > 0 else True
        vol_expansion = eod_vol >= (avg_vol_20_today * 1.2)
        quality_grade = dry_ratio >= 0.50
        
        is_ready_tomorrow = (very_near_breakout and strong_close and (eod_close > prev_close) and vol_expansion and quality_grade)

        # 2. 3D PRICE UP-SHIFTED LOGIC
        h0, h1, h2 = df.iloc[eod_idx]['High'], df.iloc[eod_idx-1]['High'], df.iloc[eod_idx-2]['High']
        l0, l1, l2 = df.iloc[eod_idx]['Low'], df.iloc[eod_idx-1]['Low'], df.iloc[eod_idx-2]['Low']
        is_3d_higher = (h0 > h1) and (h1 > h2) and (l0 > l1) and (l1 > l2)
        near_resistance = (eod_close >= box_high * 0.97) and (eod_close <= box_high * 1.015)
        v0, v1, v2 = df.iloc[eod_idx]['Volume'], df.iloc[eod_idx-1]['Volume'], df.iloc[eod_idx-2]['Volume']
        v_avg0, v_avg1, v_avg2 = df.iloc[eod_idx]['Vol_Avg_20'], df.iloc[eod_idx-1]['Vol_Avg_20'], df.iloc[eod_idx-2]['Vol_Avg_20']
        has_vol_expansion = (v0 >= v_avg0) or (v1 >= v_avg1) or (v2 >= v_avg2)
        is_uptrend_3d = is_3d_higher and near_resistance and has_vol_expansion

        # 🎯 3. NEW: SUPPORT NEARNESS + BULLISH CANDLE PATTERN LOGIC 🎯
        # Price Support level ke max 3.5% ke paas ho
        near_support = (eod_close >= pre_anchor_support * 0.99) and (eod_close <= pre_anchor_support * 1.035)
        has_bullish_pattern, pattern_name = check_bullish_candle_pattern(df, eod_idx)
        
        is_support_bullish = near_support and has_bullish_pattern
            
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
            'Pattern': pattern_name,
            'Details': f"Anchor:[{anchor_date}] | Support:{round(pre_anchor_support,1)} | DryDays:{dry_up_days}/{total_base_days}",
            'Ready_Tomorrow': is_ready_tomorrow,
            'UpTrend_3D': is_uptrend_3d,
            'Support_Bullish': is_support_bullish
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
uptrend_3d_watchlist = []
support_bullish_watchlist = []

REJECT_KEYWORDS = ['LIQUID', 'ETF', 'CPSE', 'NETF', 'GILT', 'GOLD', 'SILVER']

print(f"\n=== SCANNING {len(stocks)} STOCKS FOR EOD MULTI-SETUP ENGINE ===", flush=True)

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
            
            # 1. Ready For Tomorrow Sheet
            if setup['Ready_Tomorrow']:
                clean_ready = setup.copy()
                for k in ['Ready_Tomorrow', 'UpTrend_3D', 'Support_Bullish', 'Pattern']: clean_ready.pop(k, None)
                ready_today_watchlist.append(clean_ready)
            
            # 2. 3D Up-Shifted Sheet
            if setup['UpTrend_3D']:
                clean_3d = setup.copy()
                for k in ['Ready_Tomorrow', 'UpTrend_3D', 'Support_Bullish', 'Pattern']: clean_3d.pop(k, None)
                uptrend_3d_watchlist.append(clean_3d)

            # 🎯 3. Support Nearness + Bullish Reversal Candle Sheet
            if setup['Support_Bullish']:
                clean_supp = setup.copy()
                for k in ['Ready_Tomorrow', 'UpTrend_3D', 'Support_Bullish']: clean_supp.pop(k, None)
                support_bullish_watchlist.append(clean_supp)

            for k in ['Ready_Tomorrow', 'UpTrend_3D', 'Support_Bullish', 'Pattern']: setup.pop(k, None)
            pre_dhamaka_watchlist.append(setup)

        time.sleep(0.15)
    except Exception as e:
        pass

std_columns = ['Stock', 'Grade', 'Current_Close', 'Trigger_Above', 'Pre_Anchor_SL', 'Target', 'RR', 'Details']
support_columns = ['Stock', 'Grade', 'Pattern', 'Current_Close', 'Pre_Anchor_SL', 'Trigger_Above', 'Target', 'RR', 'Details']

# Uploading to 4 Worksheets
upload_to_sheet(ws_dhamaka_watch, pre_dhamaka_watchlist, std_columns, "No Vol-Dry Squeeze Stock Found Today")
upload_to_sheet(ws_ready_today, ready_today_watchlist, std_columns, "Agle din entry ke liye koi Strict Box High Breakout stock nahi mila.")
upload_to_sheet(ws_uptrend_3d, uptrend_3d_watchlist, std_columns, "Price Up-Shifted aur Resistance Zone ke paas koi stock nahi mila.")
upload_to_sheet(ws_support_bullish, support_bullish_watchlist, support_columns, "Support zone ke paas koi Bullish Reversal pattern nahi mila.")

print("\n=== EOD SYSTEM EXECUTION COMPLETED SUCCESSFULLY ===", flush=True)
