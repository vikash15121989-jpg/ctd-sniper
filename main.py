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
print("=== V400_BACKTEST: SMART MONEY CONCEPT BACKTESTER ===", flush=True)
print("=========================================================", flush=True)

# ===== CONFIG =====
BACKTEST_DAYS = 45  # पिछले 45 दिनों के डेटा में सिग्नल ढूंढेगा
LOOKAHEAD_DAYS = 3  # सिग्नल मिलने के बाद अगले 3 दिनों का मैक्सिमम मूव ट्रैक करेगा

MIN_AVG_VOLUME = 100000
MIN_AVG_TURNOVER_CR = 5
MIN_DATA_DAYS = 50

# ===== GOOGLE SHEETS SETUP (केवल वॉचलिस्ट से स्टॉक नाम पढ़ने के लिए) =====
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
print("Fetching data and running analysis... कृपया प्रतीक्षा करें...\n", flush=True)

# 1 साल का डेटा डाउनलोड करेंगे ताकि 20 MA सही से बने
end_date = (datetime.now() + timedelta(days=1)).date()
start_date = end_date - timedelta(days=365)

for stock in stocks:
    try:
        symbol_clean = stock.replace('.NS', '')
        df = yf.download(stock, start=start_date, end=end_date, progress=False, auto_adjust=True)
        df = flatten_yf_columns(df)

        if df.empty or len(df) < MIN_DATA_DAYS:
            continue

        # इंडिकेटर्स की गणना
        df['Avg_Vol'] = df['Volume'].rolling(window=20).mean()
        df['Avg_Turnover'] = (df['Close'] * df['Volume']).rolling(window=20).mean() / 10000000

        total_rows = len(df)
        # पिछले 45 दिनों के दायरे में हर एक दिन पर लॉजिक चेक करेंगे
        start_idx = max(MIN_DATA_DAYS, total_rows - BACKTEST_DAYS)

        for idx in range(start_idx, total_rows - LOOKAHEAD_DAYS):
            current_close = df.iloc[idx]['Close']
            current_high = df.iloc[idx]['High']
            current_low = df.iloc[idx]['Low']
            current_vol = df.iloc[idx]['Volume']
            prev_close = df.iloc[idx-1]['Close']
            vol_20ma = df.iloc[idx]['Avg_Vol']
            avg_turnover = df.iloc[idx]['Avg_Turnover']

            if pd.isna(vol_20ma) or vol_20ma == 0 or current_vol == 0: 
                continue
            if vol_20ma < MIN_AVG_VOLUME or avg_turnover < MIN_AVG_TURNOVER_CR:
                continue

            # --- 🎯 कड़े स्मार्ट मनी नियम 🎯 ---
            # 1. वॉल्यूम शॉक (3.5 गुना से ज्यादा)
            is_institutional_vol = current_vol >= (vol_20ma * 3.5)
            
            # 2. प्राइस एब्जॉर्प्शन (क्लोज पिछले 5 दिन के क्लोज एवरेज से ऊपर)
            avg_close_5d = df['Close'].iloc[max(0, idx-5):idx].mean()
            is_price_absorbed = current_close > avg_close_5d

            # 3. मजबूत क्लोजिंग (ऊपरी 30% हिस्से में क्लोज)
            candle_range = current_high - current_low
            is_smart_close = False
            if candle_range > 0:
                is_smart_close = (current_high - current_close) / candle_range <= 0.30

            # 4. शांत प्राइस मूव (सिर्फ 1% से 5% के बीच की बढ़त)
            daily_gain = ((current_close - prev_close) / prev_close) * 100
            is_valid_range = 1.0 <= daily_gain <= 5.0

            # अगर सारे नियम पास हुए, तो यह "सिग्नल का दिन" है
            if is_institutional_vol and is_price_absorbed and is_smart_close and is_valid_range:
                signal_date = df.index[idx].strftime('%Y-%m-%d')
                
                # --- 🔍 अब अगले 3 दिनों का सच ट्रैक करेंगे 🔍 ---
                future_df = df.iloc[idx+1 : idx+1+LOOKAHEAD_DAYS]
                
                # सिग्नल वाले दिन के क्लोज से अगले 3 दिनों का सबसे उच्चतम स्तर (Max High)
                max_future_high = future_df['High'].max()
                
                # कितना मैक्सिमम रिटर्न मिला (%)
                max_move_pct = ((max_future_high - current_close) / current_close) * 100
                
                # पहले दिन (T+1) का ओपन और लो (यह चेक करने के लिए कि क्या एंट्री मिली)
                next_day_open = future_df.iloc[0]['Open']
                next_day_low = future_df.iloc[0]['Low']

                all_signals.append({
                    'Stock': symbol_clean,
                    'Signal_Date': signal_date,
                    'Vol_Shock': round(current_vol / vol_20ma, 1),
                    'Signal_Gain%': round(daily_gain, 1),
                    'Close_Price': round(current_close, 2),
                    'Next_3D_Max_Move%': round(max_move_pct, 1)
                })

        time.sleep(0.05)
    except Exception as e:
        pass

# ===== रिजल्ट प्रिंट करना =====
print("\n=================== BACKTEST RESULTS ===================", flush=True)
if all_signals:
    backtest_df = pd.DataFrame(all_signals)
    # तारीख के हिसाब से सॉर्ट करें
    backtest_df.sort_values(by='Signal_Date', ascending=False, inplace=True)
    
    # रिजल्ट को सुंदर टेबल फॉर्मेट में दिखाएं
    print(backtest_df.to_string(index=False), flush=True)
    
    # समरी स्टैट्स
    hit_10pct = sum(backtest_df['Next_3D_Max_Move%'] >= 10.0)
    hit_5pct = sum(backtest_df['Next_3D_Max_Move%'] >= 5.0)
    total_signals = len(backtest_df)
    
    print("\n========= SUMMARY =========", flush=True)
    print(f"Total Signals Found: {total_signals}", flush=True)
    print(f"Signals giving > 5% move within 3 days: {hit_5pct} ({round(hit_5pct/total_signals*100, 1)}%)", flush=True)
    print(f"Signals giving > 10% move (Target) within 3 days: {hit_10pct} ({round(hit_10pct/total_signals*100, 1)}%)", flush=True)
else:
    print("पिछले दिनों में इस कड़े क्राइटेरिया पर कोई स्टॉक मैच नहीं हुआ। नियम बहुत कड़े हैं!", flush=True)
print("========================================================", flush=True)
