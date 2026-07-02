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
print("=== V600: ANCHOR CANDLE ULTRA SQUEEZE BACKTESTER ===", flush=True)
print("=========================================================", flush=True)

# ===== CONFIG =====
BACKTEST_DAYS = 60  
MIN_AVG_VOLUME = 100000
MIN_AVG_TURNOVER_CR = 10
MIN_DATA_DAYS = 60

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

print(f"Total Stocks Loaded: {len(stocks)}", flush=True)
print("Running advanced time-series backtest... कृपया प्रतीक्षा करें...\n", flush=True)

end_date = (datetime.now() + timedelta(days=1)).date()
start_date = end_date - timedelta(days=365)

for stock in stocks:
    try:
        symbol_clean = stock.replace('.NS', '')
        df = yf.download(stock, start=start_date, end=end_date, progress=False, auto_adjust=True)
        df = flatten_yf_columns(df)

        if df.empty or len(df) < MIN_DATA_DAYS:
            continue

        df['Avg_Vol'] = df['Volume'].rolling(window=20).mean()
        df['Avg_Turnover'] = (df['Close'] * df['Volume']).rolling(window=20).mean() / 10000000

        total_rows = len(df)
        start_idx = max(MIN_DATA_DAYS, total_rows - BACKTEST_DAYS)

        # लूप चलाकर "Anchor Day" ढूंढेंगे
        for idx in range(start_idx, total_rows - 5): 
            current_close = df.iloc[idx]['Close']
            current_high = df.iloc[idx]['High']
            current_low = df.iloc[idx]['Low']
            current_open = df.iloc[idx]['Open']
            current_vol = df.iloc[idx]['Volume']
            
            # पिछले 20 दिनों का मैक्सिमम वॉल्यूम (आज का छोड़कर)
            past_20d = df.iloc[max(0, idx-20):idx]
            max_vol_20d = past_20d['Volume'].max()

            if pd.isna(max_vol_20d) or max_vol_20d == 0 or current_vol == 0: continue
            if df.iloc[idx]['Avg_Vol'] < MIN_AVG_VOLUME or df.iloc[idx]['Avg_Turnover'] < MIN_AVG_TURNOVER_CR: continue

            # शर्त 1: अल्ट्रा हाई वॉल्यूम (Max Vol का 1.5 गुना)
            is_ultra_vol = current_vol >= (max_vol_20d * 1.5)
            
            # शर्त 2: स्ट्रॉन्ग बुलिश कैंडल (हरी कैंडल + क्लोजिंग हाई के पास, यानी ऊपरी 25% हिस्से में)
            candle_range = current_high - current_low
            is_bullish_candle = current_close > current_open
            is_strong_close = False
            if candle_range > 0:
                is_strong_close = (current_high - current_close) / candle_range <= 0.25

            # अगर एंकर कैंडल मिल गई, तो अब अगले दिनों का कंसॉलिडेसन चेक करेंगे
            if is_ultra_vol and is_bullish_candle and is_strong_close:
                anchor_date = df.index[idx].strftime('%Y-%m-%d')
                anchor_high = current_high
                anchor_low = current_low
                
                max_deviation = 0
                squeezed_days = 0
                breakout_idx = -1
                
                # एंकर दिन के बाद अगले दिनों को चेक करें (अधिकतम 10 दिन का कंसॉलिडेसन ट्रैक करेंगे)
                for f_idx in range(idx + 1, min(idx + 12, total_rows)):
                    f_high = df.iloc[f_idx]['High']
                    f_low = df.iloc[f_idx]['Low']
                    f_close = df.iloc[f_idx]['Close']
                    f_vol = df.iloc[f_idx]['Volume']
                    f_avg_vol = df.iloc[f_idx]['Avg_Vol']
                    
                    # एंकर कैंडल के हाई और लो से डेविएशन नापें
                    dev_high = ((f_high - anchor_high) / anchor_high) * 100
                    dev_low = ((anchor_low - f_low) / anchor_low) * 100
                    current_day_dev = max(abs(dev_high), abs(dev_low))
                    
                    # अगर प्राइस एंकर कैंडल के दायरे से 10% से ज्यादा भटक गया, तो लूप तोड़ो (फेल)
                    if current_day_dev > 10.0:
                        break
                    
                    # डेविएशन ट्रैक करते रहें (जितना कम, उतना बेहतर)
                    if current_day_dev > max_deviation:
                        max_deviation = current_day_dev
                        
                    squeezed_days += 1
                    
                    # ब्रेकआउट ट्रिगर: अगर प्राइस एंकर हाई के ऊपर क्लोज दे और वॉल्यूम भी ऐवरेज से ऊपर हो
                    if f_close > anchor_high and f_vol > f_avg_vol:
                        breakout_idx = f_idx
                        break
                
                # अगर हमें एक जायज स्क्वीज पीरियड (कम से कम 2 दिन फंसा रहा) और ब्रेकआउट मिला:
                if breakout_idx != -1 and squeezed_days >= 2:
                    b_close = df.iloc[breakout_idx]['Close']
                    b_prev_close = df.iloc[breakout_idx-1]['Close']
                    b_date = df.index[breakout_idx].strftime('%Y-%m-%d')
                    
                    # ब्रेकआउट वाले दिन का असली % मूव
                    breakout_move = ((b_close - b_prev_close) / b_prev_close) * 100
                    
                    all_signals.append({
                        'Stock': symbol_clean,
                        'Anchor_Date': anchor_date,
                        'Breakout_Date': b_date,
                        'Squeeze_Days': squeezed_days,
                        'Max_Dev%': round(max_deviation, 1),
                        'Breakout_Move%': round(breakout_move, 1)
                    })
                    
                # इंडेक्स को आगे बढ़ाएं ताकि बार-बार ओवरलैपिंग सिग्नल्स न आएं
                idx += squeezed_days

        time.sleep(0.01)
    except Exception as e:
        pass

# ===== रिजल्ट प्रिंट करना =====
print("\n=================== TIME-SERIES BACKTEST RESULTS ===================", flush=True)
if all_signals:
    backtest_df = pd.DataFrame(all_signals)
    backtest_df.sort_values(by='Max_Dev%', ascending=True, inplace=True) # कम डेविएशन वाले ऊपर दिखेंगे
    print(backtest_df.to_string(index=False), flush=True)
    
    total_signals = len(backtest_df)
    blast_10 = sum(backtest_df['Breakout_Move%'] >= 9.5)
    blast_5 = sum(backtest_df['Breakout_Move%'] >= 4.5)
    
    print("\n========= SUMMMARY =========", flush=True)
    print(f"Total Quality Squeeze Setups Found: {total_signals}", flush=True)
    print(f"Signals with > 5% Blast on Breakout Day: {blast_5} ({round(blast_5/total_signals*100, 1)}%)", flush=True)
    print(f"Signals with > 10% Jackpot on Breakout Day: {blast_10} ({round(blast_10/total_signals*100, 1)}%)", flush=True)
else:
    print("इस अनोखे टाइम-सीरीज़ लॉजिक पर कोई स्टॉक मैच नहीं हुआ।", flush=True)
print("========================================================", flush=True)
