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

print("=========================================================", flush=True)
print("=== V800: ULTRA VOL + BASE SQUEEZE + VOLUME DRY-UP ===", flush=True)
print("=========================================================", flush=True)

# ===== CONFIG =====
BACKTEST_DAYS = 90  
MIN_AVG_VOLUME = 100000
MIN_AVG_TURNOVER_CR = 10

# ===== GOOGLE SHEETS SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

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

# ===== BACKTEST EXECUTION =====
stocks = get_watchlist_stocks()
all_signals = []

print(f"Total Stocks Loaded from Watchlist: {len(stocks)}", flush=True)

end_date = (datetime.now() + timedelta(days=1)).date()
start_date = end_date - timedelta(days=365)

for stock in stocks:
    try:
        symbol_clean = stock.replace('.NS', '')
        df = yf.download(stock, start=start_date, end=end_date, progress=False, auto_adjust=True)
        df = flatten_yf_columns(df)

        if df.empty or len(df) < 60: continue

        df['Avg_Vol'] = df['Volume'].rolling(window=20).mean()
        df['Avg_Turnover'] = (df['Close'] * df['Volume']).rolling(window=20).mean() / 10000000

        total_rows = len(df)
        start_idx = max(60, total_rows - BACKTEST_DAYS)

        # 1. लूप चलाकर "Ultra Volume Day" ढूंढेंगे
        for idx in range(start_idx, total_rows - 7):
            current_close = df.iloc[idx]['Close']
            current_vol = df.iloc[idx]['Volume']
            
            past_20d = df.iloc[max(0, idx-20):idx]
            max_vol_20d = past_20d['Volume'].max()

            if pd.isna(max_vol_20d) or max_vol_20d == 0: continue
            if df.iloc[idx]['Avg_Vol'] < MIN_AVG_VOLUME or df.iloc[idx]['Avg_Turnover'] < MIN_AVG_TURNOVER_CR: continue

            # शर्त 1: अल्ट्रा हाई वॉल्यूम आया (Max Vol से 1.5x ऊपर)
            if current_vol >= (max_vol_20d * 1.5):
                anchor_date = df.index[idx].strftime('%Y-%m-%d')
                anchor_close = current_close
                anchor_vol = current_vol
                
                is_base_valid = True
                dry_up_days = 0
                breakout_idx = -1
                
                # 2. अब आगे के दिनों में 'Base Formation' और 'Volume Contraction' चेक करेंगे
                # कम से कम 4 दिन और अधिकतम 15 दिन का बेस ट्रैक करेंगे
                for f_idx in range(idx + 1, min(idx + 16, total_rows)):
                    f_close = df.iloc[f_idx]['Close']
                    f_vol = df.iloc[f_idx]['Volume']
                    f_avg_vol = df.iloc[f_idx]['Avg_Vol']
                    
                    # शर्त 2: प्राइस डेविएशन (एंकर क्लोज से सिर्फ 5% के अंदर टाइट बेस बनाना चाहिए)
                    price_deviation = (abs(f_close - anchor_close) / anchor_close) * 100
                    if price_deviation > 5.0:
                        is_base_valid = False
                        break
                    
                    # शर्त 3: वॉल्यूम लगातार सिकुड़ना चाहिए (Volume Dry-up)
                    # यहाँ चेक कर रहे हैं कि क्या वॉल्यूम उस अल्ट्रा वॉल्यूम के 30% से भी कम रह गया है
                    if f_vol < (anchor_vol * 0.30):
                        dry_up_days += 1
                    
                    # 3. द ट्रिगर: दोबारा वॉल्यूम फटना (Breakout)
                    # अगर प्राइस बेस लेवल के ऊपर निकले और वॉल्यूम अचानक अपने 20 MA से 1.8 गुना ऊपर आ जाए
                    if f_close > anchor_close and f_vol >= (f_avg_vol * 1.8) and (f_idx - idx) >= 4:
                        breakout_idx = f_idx
                        break
                
                # अगर बेस मजबूत रहा, वॉल्यूम ड्राई-अप दिखा, और फिर वॉल्यूम फटा:
                if is_base_valid and breakout_idx != -1 and dry_up_days >= 2:
                    b_close = df.iloc[breakout_idx]['Close']
                    b_prev_close = df.iloc[breakout_idx-1]['Close']
                    b_date = df.index[breakout_idx].strftime('%Y-%m-%d')
                    b_vol = df.iloc[breakout_idx]['Volume']
                    b_avg_vol = df.iloc[breakout_idx]['Avg_Vol']
                    
                    # फटने वाले दिन का असली % मूव
                    breakout_move = ((b_close - b_prev_close) / b_prev_close) * 100
                    base_length = breakout_idx - idx
                    
                    all_signals.append({
                        'Stock': symbol_clean,
                        'Ultra_Vol_Date': anchor_date,
                        'Breakout_Date': b_date,
                        'Base_Days': base_length,
                        'DryUp_Days': dry_up_days,
                        'Vol_Burst': round(b_vol / b_avg_vol, 1),
                        'Blast_Move%': round(breakout_move, 1)
                    })
                    idx += base_length  # इंडेक्स आगे बढ़ाएं

        time.sleep(0.01)
    except Exception as e:
        pass

# ===== रिजल्ट प्रिंट करना =====
print("\n=================== VCP & DRY-UP RESULTS ===================", flush=True)
if all_signals:
    backtest_df = pd.DataFrame(all_signals)
    backtest_df.sort_values(by='Blast_Move%', ascending=False, inplace=True) # सबसे बड़े धमाके ऊपर दिखेंगे
    print(backtest_df.to_string(index=False), flush=True)
    
    total_signals = len(backtest_df)
    hit_10 = sum(backtest_df['Blast_Move%'] >= 9.5)
    hit_5 = sum(backtest_df['Blast_Move%'] >= 4.5)
    
    print("\n========= SUMMARY =========", flush=True)
    print(f"Total Squeeze + DryUp Setups: {total_signals}", flush=True)
    print(f"Signals with > 5% Move on Blast Day: {hit_5} ({round(hit_5/total_signals*100, 1)}%)", flush=True)
    print(f"Signals with > 10% Move (Target) on Blast Day: {hit_10} ({round(hit_10/total_signals*100, 1)}%)", flush=True)
else:
    print("इस कड़े वॉल्यूम कॉन्ट्रैक्शन और ड्राई-अप लॉजिक पर कोई स्टॉक मैच नहीं हुआ। नियम बहुत कड़े हैं!", flush=True)
print("========================================================", flush=True)
