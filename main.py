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
print("=== V1300: ENDLESS BASE VCP (NO TIME LIMIT TRACKER) ===", flush=True)
print("=========================================================", flush=True)

# ===== CONFIG =====
BACKTEST_DAYS = 120            # बैकटेस्ट की अवधि थोड़ी बढ़ा दी ताकि पुराने बेस भी दिखें
MIN_AVG_VOLUME = 100000
MIN_AVG_TURNOVER_CR = 10
LOOKBACK_ULTRA_VOL = 50        # Day 0 के लिए 50-Day Absolute Max Volume

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
print("बिना किसी टाइम लिमिट के ओपन बेस स्कैनिंग शुरू हो रही है...\n", flush=True)

end_date = (datetime.now() + timedelta(days=1)).date()
start_date = end_date - timedelta(days=365)

for stock in stocks:
    try:
        symbol_clean = stock.replace('.NS', '')
        df = yf.download(stock, start=start_date, end=end_date, progress=False, auto_adjust=True)
        df = flatten_yf_columns(df)

        if df.empty or len(df) < 100: continue

        df['Avg_Vol'] = df['Volume'].rolling(window=20).mean()
        df['Avg_Turnover'] = (df['Close'] * df['Volume']).rolling(window=20).mean() / 10000000

        total_rows = len(df)
        start_idx = max(100, total_rows - BACKTEST_DAYS)

        idx = start_idx
        while idx < total_rows - 2:
            current_close = df.iloc[idx]['Close']
            current_open = df.iloc[idx]['Open']
            current_vol = df.iloc[idx]['Volume']
            
            past_50d = df.iloc[max(0, idx-LOOKBACK_ULTRA_VOL):idx]
            absolute_max_vol_50d = past_50d['Volume'].max()

            if pd.isna(absolute_max_vol_50d) or absolute_max_vol_50d == 0:
                idx += 1
                continue

            # 1. Day 0 का ट्रिगर (50-Day Max Volume + Bullish Close)
            if current_vol > absolute_max_vol_50d and current_close > current_open:
                anchor_date = df.index[idx].strftime('%Y-%m-%d')
                anchor_close = current_close
                anchor_vol = current_vol
                
                dry_up_days_count = 0
                breakout_idx = -1
                f_idx = idx + 1
                
                # 2. 🚨 अनंत लूप (जब तक नियम न टूटे, चेक करते रहो)
                while f_idx < total_rows:
                    f_close = df.iloc[f_idx]['Close']
                    f_vol = df.iloc[f_idx]['Volume']
                    f_avg_vol = df.iloc[f_idx]['Avg_Vol']
                    
                    # नियम टूटने की शर्त: अगर प्राइस Day 0 के क्लोज से 5% से ज़्यादा नीचे टूट गया, तो बेस फेल!
                    if f_close < (anchor_close * 0.95):
                        break
                        
                    # अगर प्राइस ऊपर की तरफ 8% से ज़्यादा भाग गया बिना वॉल्यूम के, तो भी सेटअप इनवैलिड
                    if f_close > (anchor_close * 1.08) and f_vol < (f_avg_vol * 1.5):
                        break
                    
                    # वॉल्यूम ड्राई-अप की गिनती चालू रखो
                    if f_vol < (anchor_vol * 0.25):
                        dry_up_days_count += 1
                    
                    # 3. असली ब्लास्ट डे का ट्रिगर (जब भी वॉल्यूम 1.8x फटे और क्लोजिंग ऊपर हो)
                    if f_close > anchor_close and f_vol >= (f_avg_vol * 1.8) and (f_idx - idx) >= 3:
                        breakout_idx = f_idx
                        break
                        
                    f_idx += 1
                
                # अगर बेस के दौरान कम से कम 2 दिन वॉल्यूम सूखा था और ब्लास्ट मिला
                if breakout_idx != -1 and dry_up_days_count >= 2:
                    b_close = df.iloc[breakout_idx]['Close']
                    b_prev_close = df.iloc[breakout_idx-1]['Close']
                    b_date = df.index[breakout_idx].strftime('%Y-%m-%d')
                    b_vol = df.iloc[breakout_idx]['Volume']
                    b_avg_vol = df.iloc[breakout_idx]['Avg_Vol']
                    
                    breakout_move = ((b_close - b_prev_close) / b_prev_close) * 100
                    base_duration = breakout_idx - idx
                    
                    all_signals.append({
                        'Stock': symbol_clean,
                        'Day0_Vol_Date': anchor_date,
                        'Blast_Date': b_date,
                        'Base_Days': base_duration,  # यह अब 10, 20, 30 कुछ भी हो सकता है
                        'DryUp_Days': dry_up_days_count,
                        'Blast_Vol_X': round(b_vol / b_avg_vol, 1),
                        'Blast_Move%': round(breakout_move, 1)
                    })
                    idx = breakout_idx  # सीधे ब्लास्ट वाले इंडेक्स पर कूदें
                    continue

            idx += 1
        time.sleep(0.01)
    except Exception as e:
        pass

# ===== रिजल्ट प्रिंट करना =====
print("\n=================== ENDLESS BASE RESULTS ===================", flush=True)
if all_signals:
    backtest_df = pd.DataFrame(all_signals)
    backtest_df.sort_values(by='Blast_Move%', ascending=False, inplace=True)
    print(backtest_df.to_string(index=False), flush=True)
else:
    print("इस ओपन-एंडेड वॉल्यूम कॉन्ट्रैक्शन लॉजिक पर कोई स्टॉक मैच नहीं हुआ।", flush=True)
print("========================================================", flush=True)
