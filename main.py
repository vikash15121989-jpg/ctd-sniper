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

print("=== V100.4: COMSYN LIVE ANCHOR & SQUEEZE GRADING ENGINE ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== CONFIG =====
END_DATE = (datetime.now() + timedelta(days=1)).date()
START_DATE = END_DATE - timedelta(days=365)

MIN_AVG_VOLUME = 100000
MIN_AVG_TURNOVER_CR = 5
LOOKBACK_ULTRA_VOL = 50        # Day 0 ढूंढने का पैमाना

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

# ===== 🎯 COMSYN LIVE ANCHOR & SQUEEZE ENGINE 🎯 =====
def scan_live_comsyn_base(df):
    total_rows = len(df)
    if total_rows < 60: return None

    live_idx = total_rows - 1
    live_close = df.iloc[live_idx]['Close']
    
    # पिछले 40 दिनों में मुड़कर सबसे लेटेस्ट वैलिड 'Day 0' (Anchor) खोजना
    found_anchor = False
    anchor_date = None
    anchor_close = 0
    anchor_vol = 0
    anchor_row_idx = -1
    
    for idx in range(live_idx - 40, live_idx - 1):
        if idx < LOOKBACK_ULTRA_VOL: continue
        
        check_vol = df.iloc[idx]['Volume']
        check_close = df.iloc[idx]['Close']
        check_open = df.iloc[idx]['Open']
        
        past_50d = df.iloc[idx-LOOKBACK_ULTRA_VOL:idx]
        max_vol_50d = past_50d['Volume'].max()
        
        # 50-Day Absolute Max Volume + Bullish Green Candle
        if check_vol > max_vol_50d and check_close > check_open:
            anchor_date = df.index[idx].strftime('%d-%b')
            anchor_close = check_close
            anchor_vol = check_vol
            anchor_row_idx = idx
            found_anchor = True

    if found_anchor:
        is_base_alive = True
        dry_up_days = 0
        prices_in_base = []
        
        # एंकर बनने के बाद से आज तक के पूरे बेस का ट्रैक रिकॉर्ड निकालना
        for check_idx in range(anchor_row_idx + 1, total_rows):
            f_close = df.iloc[check_idx]['Close']
            f_vol = df.iloc[check_idx]['Volume']
            prices_in_base.append(f_close)
            
            # स्टॉपलॉस नियम: अगर प्राइस कभी भी एंकर क्लोज से 5% से नीचे गया, तो बेस फेल!
            if f_close < (anchor_close * 0.95):
                is_base_alive = False
                break
            
            # वॉल्यूम ड्राई-अपकाउंटर (Anchor Vol के 25% से कम)
            if f_vol < (anchor_vol * 0.25):
                dry_up_days += 1
                
        if is_base_alive and len(prices_in_base) >= 2:
            base_min = min(prices_in_base)
            base_max = max(prices_in_base)
            # बेस का कुल वास्तविक दायरा (Contract Range %)
            current_range_pct = ((base_max - base_min) / base_min) * 100
            
            base_days_count = live_idx - anchor_row_idx
            
            # 🎯 VIKASH SMART AUTO-GRADING MATRIX (FOR COMSYN SQUEEZE) 🎯
            if current_range_pct <= 3.5 and dry_up_days >= 4:
                grade = "GRADE A+ (Ultra Sniper Squeeze)"
            elif current_range_pct <= 5.0 and dry_up_days >= 2:
                grade = "GRADE A (High Vol Base Setup)"
            else:
                grade = "GRADE B (Watch Closely)"
                
            # SL and Target
            stop_loss = round(anchor_close * 0.95, 2)
            target_1 = round(live_close * 1.15, 2)
            
            risk = max(0.01, live_close - stop_loss)
            reward = target_1 - live_close

            return {
                'Stock': '', 
                'Grade': grade, 
                'Current_Close': round(live_close, 2),
                'Buy_Level': f"Near {round(anchor_close, 2)}",
                'StopLoss': stop_loss,
                'Target': target_1,
                'RR': round(reward/risk, 1),
                'Details': f"Anchor:[{anchor_date}] | BaseDays:{base_days_count} | Range:{round(current_range_pct, 1)}% | DryDays:{dry_up_days}"
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
            
            # ग्रेड के हिसाब से सॉर्टिंग (A+ ऊपर चमकेगा)
            df.sort_values(by=['Grade'], ascending=True, inplace=True)
            
            df_json = json.loads(df.to_json(orient='split'))
            values = [df_json['columns']] + df_json['data']
            ws.update(values=values, range_name='A1')
            print(f"Uploaded {len(data_list)} sorted rows to Pre_Dhamaka_Watch.", flush=True)
        else:
            ws.update(values=[[default_msg]], range_name='A1')
    except Exception as e:
        print(f"Sheet Error: {str(e)}", flush=True)

# ===== MAIN EXECUTION LOOP =====
stocks = get_watchlist_stocks()
final_dhamaka_watchlist = []

REJECT_KEYWORDS = ['LIQUID', 'ETF', 'CPSE', 'NETF', 'GILT', 'GOLD', 'SILVER']

print(f"\n=== SCANNINIG {len(stocks)} STOCKS FOR LIVE COMSYN BASE ===", flush=True)

for i, stock in enumerate(stocks):
    try:
        symbol_clean = stock.replace('.NS', '')
        
        if any(keyword in symbol_clean for keyword in REJECT_KEYWORDS):
            continue

        stock_df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=True)
        stock_df = flatten_yf_columns(stock_df)

        if stock_df.empty or len(stock_df) < 60:
            continue

        stock_df['Avg_Vol'] = stock_df['Volume'].rolling(window=20).mean()
        stock_df['Avg_Turnover'] = (stock_df['Close'] * stock_df['Volume']).rolling(window=20).mean() / 10000000

        curr_idx = len(stock_df) - 1
        avg_vol = stock_df.iloc[curr_idx]['Avg_Vol']
        avg_turnover = stock_df.iloc[curr_idx]['Avg_Turnover']

        if pd.isna(avg_vol) or pd.isna(avg_turnover) or avg_vol < MIN_AVG_VOLUME or avg_turnover < MIN_AVG_TURNOVER_CR:
            continue

        setup = scan_live_comsyn_base(stock_df)
        if setup:
            setup['Stock'] = symbol_clean
            final_dhamaka_watchlist.append(setup)
            print(f"🔥 LIVE MATCH! [{symbol_clean}] -> {setup['Grade']}", flush=True)

        time.sleep(0.15)
    except Exception as e:
        pass

columns = ['Stock', 'Grade', 'Current_Close', 'Buy_Level', 'StopLoss', 'Target', 'RR', 'Details']
upload_to_sheet(ws_dhamaka_watch, final_dhamaka_watchlist, columns, "No Live COMSYN Squeeze Found Today")
print("\n=== SYSTEM EXECUTION COMPLETED ===", flush=True)
