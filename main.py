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

print("=== V41.5: REAL BUGS FIXED ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== CONFIG =====
END_DATE = datetime.now().date()
START_DATE = END_DATE - timedelta(days=365)

MIN_AVG_VOLUME = 100000
MIN_AVG_TURNOVER_CR = 5
SWING_LENGTH = 5
PULLBACK_ZONE_PCT = 3.0
BREAKOUT_BUFFER_PCT = 5.0 

# ===== GOOGLE SHEETS SETUP =====
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

try:
    ws_sniper = sh.worksheet("SWING_PULLBACK_SNIPER")
except gspread.exceptions.WorksheetNotFound:
    ws_sniper = sh.add_worksheet(title="SWING_PULLBACK_SNIPER", rows="1000", cols="20")

print("Sniper Worksheet connected.", flush=True)

def get_watchlist_stocks():
    stocks = ws_watchlist.col_values(1)
    stocks = [s.strip().upper() for s in stocks if s.strip() and s.strip().upper() not in ['STOCK', 'SYMBOL', 'NAME']]
    stocks = [s + '.NS' if not s.endswith('.NS') and not s.startswith('^') else s for s in stocks]
    return stocks

def flatten_yf_columns(df):
    """FIX: MultiIndex handling strictly and safely"""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    
    # Ensure correct casing
    df.columns = [str(col).strip().title() for col in df.columns]
    
    if 'Adj Close' in df.columns and 'Close' not in df.columns:
        df['Close'] = df['Adj Close']
    return df

def get_swing_levels(df, idx, length=5):
    """FIX: Future leak completely removed. Rolling logic corrected without center=True lookahead."""
    if idx < length * 2:
        return None

    # Slice strictly till current index (no future data)
    df_window = df.iloc[max(0, idx - 150):idx+1].copy()
    window_size = length * 2 + 1

    # Safe Pivot detection using shifting
    ph_mask = (df_window['High'].shift(length) == df_window['High'].rolling(window_size).max()).shift(-length).fillna(False)
    pl_mask = (df_window['Low'].shift(length) == df_window['Low'].rolling(window_size).min()).shift(-length).fillna(False)

    pivot_highs = df_window[ph_mask]['High'].dropna().tail(2)
    pivot_lows = df_window[pl_mask]['Low'].dropna().tail(2)

    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return None

    return {
        'latest_ph': pivot_highs.iloc[-1],
        'prev_ph': pivot_highs.iloc[-2],
        'latest_pl': pivot_lows.iloc[-1],
        'prev_pl': pivot_lows.iloc[-2]
    }

def check_choch_major_bottom(df, idx, lookback=60):
    if idx < lookback:
        return False, None

    window = df.iloc[idx-lookback:idx+1]
    major_bottom_idx = window['Low'].idxmin()
    major_bottom_price = window.loc[major_bottom_idx, 'Low']
    major_bottom_loc = df.index.get_loc(major_bottom_idx)

    if df.iloc[idx]['Close'] < major_bottom_price:
        return False, None

    post_bottom_df = df.iloc[major_bottom_loc:idx+1]
    if post_bottom_df['Low'].min() < major_bottom_price * 0.99:
        return False, None

    return True, major_bottom_price

def check_hh_hl_swing_structure(swing_data, current_close):
    if swing_data is None:
        return False

    hh = swing_data['latest_ph'] > swing_data['prev_ph']
    hl = swing_data['latest_pl'] > swing_data['prev_pl']
    uptrend = current_close > swing_data['latest_pl']

    return hh and hl and uptrend

def check_pullback_to_swing_support(df, idx, swing_data):
    if swing_data is None:
        return False, None, None

    current_close = df.iloc[idx]['Close']
    latest_ph = swing_data['latest_ph']
    prev_ph = swing_data['prev_ph']
    prev_pl = swing_data['prev_pl']

    if current_close > latest_ph * (1 + BREAKOUT_BUFFER_PCT/100):
        return False, None, None

    dist_to_prev_ph = abs((current_close - prev_ph) / prev_ph) * 100
    if dist_to_prev_ph <= PULLBACK_ZONE_PCT:
        return True, "Prev_Swing_High", prev_ph

    dist_to_prev_pl = abs((current_close - prev_pl) / prev_pl) * 100
    if dist_to_prev_pl <= PULLBACK_ZONE_PCT:
        return True, "Prev_Swing_Low", prev_pl

    return False, None, None

def check_strength(df, idx):
    if idx < 2:
        return False

    current_green = df.iloc[idx]['Close'] > df.iloc[idx]['Open']
    volume_up = df.iloc[idx]['Volume'] > df.iloc[idx-1]['Volume']

    last_3 = df.iloc[idx-2:idx+1]
    green_count = len(last_3[last_3['Close'] > last_3['Open']])

    return current_green and volume_up and green_count >= 2

def analyze_stock(df, stock):
    df = df.dropna(subset=['Close', 'High', 'Low', 'Volume']).copy()
    if len(df) < 80:
        return None

    df['Avg_Vol'] = df['Volume'].rolling(window=20).mean()
    df['Avg_Turnover'] = (df['Close'] * df['Volume']).rolling(window=20).mean() / 10000000

    idx = len(df) - 1
    row = df.iloc[idx]

    if row['Avg_Vol'] < MIN_AVG_VOLUME or row['Avg_Turnover'] < MIN_AVG_TURNOVER_CR:
        return None

    choch_ok, major_bottom = check_choch_major_bottom(df, idx)
    if not choch_ok:
        return None

    swing_data = get_swing_levels(df, idx, SWING_LENGTH)
    if swing_data is None:
        return None

    hh_hl_ok = check_hh_hl_swing_structure(swing_data, row['Close'])
    if not hh_hl_ok:
        return None

    pullback_ok, pullback_to, support_level = check_pullback_to_swing_support(df, idx, swing_data)
    if not pullback_ok:
        return None

    if not check_strength(df, idx):
        return None

    return {
        'Signal_Date': str(df.index[idx].date()),
        'Stock': stock.replace('.NS', ''),
        'Close': float(round(row['Close'], 2)),
        'Major_Bottom': float(round(major_bottom, 2)),
        'Latest_Swing_High': float(round(swing_data['latest_ph'], 2)),
        'Prev_Swing_High': float(round(swing_data['prev_ph'], 2)),
        'Prev_Swing_Low': float(round(swing_data['prev_pl'], 2)),
        'Pullback_To': pullback_to,
        'Support_Level': float(round(support_level, 2)),
        'Distance_From_Support_Pct': float(round(((row['Close'] - support_level) / support_level) * 100, 2)),
        'Volume': int(row['Volume']),
        'Avg_Volume': int(row['Avg_Vol']),
        'Entry_Above': float(round(swing_data['latest_ph'] * 1.001, 2)),
        'Stop_Loss': float(round(support_level * 0.99, 2)),
        'Risk_Pct': float(round(((row['Close'] - support_level * 0.99) / row['Close']) * 100, 2))
    }

# ===== MAIN EXECUTION =====
stocks = get_watchlist_stocks()
all_signals = []

print(f"\n=== SCANNING {len(stocks)} STOCKS ===", flush=True)
start_download_dt = START_DATE - timedelta(days=200)

for i, stock in enumerate(stocks):
    try:
        print(f"[{i+1}/{len(stocks)}] Scanning {stock}...", flush=True)

        stock_df = yf.download(stock, start=start_download_dt, end=END_DATE, progress=False, auto_adjust=False)
        stock_df = flatten_yf_columns(stock_df)

        if stock_df.empty or len(stock_df) < 80 or 'Close' not in stock_df.columns:
            print(f" -> {stock}: No data / insufficient data", flush=True)
            continue

        signal = analyze_stock(stock_df, stock)

        if signal:
            all_signals.append(signal)
            print(f" -> {stock}: SNIPER FOUND! Pullback to {signal['Pullback_To']}", flush=True)

        time.sleep(0.5)

    except Exception as e:
        print(f" -> {stock}: Error - {str(e)[:80]}", flush=True)
        continue

# ===== UPDATE SHEET =====
if all_signals:
    df_sniper = pd.DataFrame(all_signals).sort_values('Risk_Pct', ascending=True)
    ws_sniper.clear()
    time.sleep(1) 
    
    # FIX: Native types conversion before gspread upload
    df_sniper_json = json.loads(df_sniper.to_json(orient='split'))
    values_to_upload = [df_sniper_json['columns']] + df_sniper_json['data']
    
    ws_sniper.update(values=values_to_upload, range_name='A1')
    print(f"\n=== FOUND {len(df_sniper)} SWING PULLBACK SETUPS ===", flush=True)
else:
    ws_sniper.clear()
    time.sleep(1)
    ws_sniper.update(values=[['Aaj koi Swing Pullback Setup nahi mila']], range_name='A1')
    print(f"\n=== NO SETUPS TODAY ===", flush=True)
    
