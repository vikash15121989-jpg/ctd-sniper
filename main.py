import yfinance as yf
import pandas as pd
import numpy as np
import gspread
import json
import os
from datetime import datetime, timedelta
import time
import warnings
warnings.filterwarnings('ignore')

print("=== CTD SNIPER V15.21 - CLIMAX ADDED ===", flush=True)

# 1. SETUP
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

end_date = datetime.now()
start_date = end_date - timedelta(days=400)
lookback_days = 10

print(f"Backfill: {lookback_days} trading days till {end_date.date()}", flush=True)

# 2. NIFTY REGIME CHECK
nifty = yf.download("^NSEI", start=start_date - timedelta(days=250), end=end_date + timedelta(days=1), progress=False)
if isinstance(nifty.columns, pd.MultiIndex):
    nifty.columns = nifty.columns.droplevel(1)
if nifty.empty or len(nifty) < 200:
    raise ValueError("Nifty data nahi mila")

nifty['200DMA'] = nifty['Close'].rolling(200).mean()
nifty['50DMA'] = nifty['Close'].rolling(50).mean()

close_now = float(nifty['Close'].iloc[-1])
dma200_now = float(nifty['200DMA'].iloc[-1])
dma50_now = float(nifty['50DMA'].iloc[-1])

is_bull = close_now > dma200_now and dma50_now > dma200_now
regime = "BULL" if is_bull else "BEAR"
print(f"Market Regime: {regime}", flush=True)

# 3. V15.21 RULES
if regime == "BULL":
    R = {
        'score_ranges': [(80, 82), (88, 90)],
        'sl_pct': 3.0, 'target_pct': 6.0,
        'min_vol_growth': 0.85, 'max_price_drop_10d': -3.0,
    }
else:
    R = {
        'score_ranges': [(86, 90)],
        'sl_pct': 2.5, 'target_pct': 4.0,
        'min_vol_growth': 1.0, 'max_price_drop_10d': -1.0,
    }

R.update({
    'min_price': 50, 'min_daily_value_cr': 0.5, 'min_vol_shares': 100000,
    'uptrend_days': 10, 'vol_ma_days': 20,
    'accum_days': 10, 'min_green_red_ratio': 1.1,
    # CLIMAX RULES
    'sc_body_pct': 2.0, 'sc_vol_multiple': 2.0, 'sc_wick_pct': 15.0,
    'sc_pullback_min': 8.0, 'sc_pullback_max': 20.0, 'sc_lookback': 60,
    'sc_gap_days': 5, # Buying Climax ke kitne din baad SC dhoondhna hai
})

def add_indicators(df):
    df['Vol_10D_Max'] = df['Volume'].rolling(10).max().shift(1)
    df['High_10D_Max'] = df['High'].rolling(10).max().shift(1)
    df['Vol_20MA'] = df['Volume'].rolling(20).mean()
    df['Daily_Value'] = df['Close'] * df['Volume']
    df['Daily_Value_20MA'] = df['Daily_Value'].rolling(20).mean()
    df['New_High_10D'] = df['High'] > df['High'].shift(1).rolling(10).max()
    # CLIMAX HELPERS
    df['Body'] = abs(df['Close'] - df['Open']) / df['Open'] * 100
    df['Upper_Wick'] = (df['High'] - df[['Close','Open']].max(axis=1)) / (df['High'] - df['Low'] + 0.01) * 100
    df['Lower_Wick'] = (df[['Close','Open']].min(axis=1) - df['Low']) / (df['High'] - df['Low'] + 0.01) * 100
    df['Vol_1D_Ago'] = df['Volume'].shift(1)
    return df

def check_liquidity(df, idx):
    try:
        close = df['Close'].iloc[idx]
        vol_20ma = df['Vol_20MA'].iloc[idx]
        daily_val = df['Daily_Value_20MA'].iloc[idx]
        if pd.isna(close) or close < R['min_price']: return False
        if pd.isna(daily_val) or daily_val < R['min_daily_value_cr'] * 1e7: return False
        if pd.isna(vol_20ma) or vol_20ma < R['min_vol_shares']: return False
        return True
    except:
        return False

def check_uptrend(df, idx):
    uptrend_start = idx - R['uptrend_days']
    if uptrend_start < 0: return False, {}
    uptrend_zone = df.iloc[uptrend_start:idx]
    new_highs = uptrend_zone['New_High_10D'].sum()
    avg_vol = uptrend_zone['Volume'].mean()
    vol_20ma = df['Vol_20MA'].iloc[idx]
    if pd.isna(vol_20ma): return False, {}
    uptrend = new_highs >= 3 and avg_vol > vol_20ma
    if uptrend:
        return True, {
            'Date': df.index[idx].strftime('%Y-%m-%d'),
            'New_Highs_10D': int(new_highs),
            'CMP': round(df['Close'].iloc[idx], 2),
            'From_52W_High_%': round((df['High'].iloc[max(0, idx-252):idx].max() / df['Close'].iloc[idx] - 1) * 100, 1)
        }
    return False, {}

# === NAYA FUNCTION: BUYING CLIMAX CHECK ===
def check_buying_climax(df, idx):
    if idx < 1: return False
    row = df.iloc[idx]
    # Cond 1: 2%+ green body + Close near High
    cond1 = row['Body'] >= R['sc_body_pct'] and row['Close'] > row['Open'] and row['Upper_Wick'] < R['sc_wick_pct']
    # Cond 2: 2x Volume
    cond2 = row['Volume'] >= row['Vol_1D_Ago'] * R['sc_vol_multiple']
    return cond1 and cond2

# === NAYA FUNCTION: SELLING CLIMAX CHECK ===
def check_selling_climax(df, idx, bc_idx):
    if idx <= bc_idx + R['sc_gap_days']: return False, {} # BC ke 5 din baad hi check karo

    row = df.iloc[idx]
    bc_high = df['High'].iloc[bc_idx]

    # Cond 1: Pullback zone 8-20% from BC High
    pullback = (bc_high - row['Close']) / bc_high * 100
    cond1 = R['sc_pullback_min'] <= pullback <= R['sc_pullback_max']

    # Cond 2: 2%+ body + 2x Volume + Upar wick kam
    cond2 = row['Body'] >= R['sc_body_pct'] and row['Volume'] >= row['Vol_1D_Ago'] * R['sc_vol_multiple']
    cond3 = row['Upper_Wick'] < R['sc_wick_pct']

    if not (cond1 and cond2 and cond3): return False, {}

    # Cond 3: 50 DMA ke upar
    sma50 = df['Close'].rolling(50).mean().iloc[idx]
    if pd.isna(sma50) or row['Close'] < sma50: return False, {}

    # Weight: Green SC = 100, Red SC = 70
    is_green = row['Close'] > row['Open']
    weight = 100 if is_green else 70

    return True, {
        'Date': df.index[idx].strftime('%Y-%m-%d'),
        'Stock': '',
        'SC_Type': 'GREEN' if is_green else 'RED',
        'Weight': weight,
        'BC_Date': df.index[bc_idx].strftime('%Y-%m-%d'),
        'BC_High': round(bc_high, 2),
        'SC_Low': round(row['Low'], 2),
        'Pullback_%': round(pullback, 1),
        'Volume_x': round(row['Volume'] / row['Vol_1D_Ago'], 1),
        'Entry_Above': round(row['High'], 2),
        'SL': round(row['Low'] * 0.99, 2) # Low ke 1% neeche
    }

def check_silent(df, idx):
    silent_row = df.iloc[idx]
    vol_max_10d_silent = silent_row['Vol_10D_Max']
    high_max_10d_silent = silent_row['High_10D_Max']
    if pd.isna(vol_max_10d_silent) or pd.isna(high_max_10d_silent): return False, {}

    silent_cond1 = silent_row['Volume'] > vol_max_10d_silent
    silent_cond2 = silent_row['High'] < high_max_10d_silent
    if not (silent_cond1 and silent_cond2): return False, {}

    acc_start = max(0, idx - R['accum_days'])
    acc_zone = df.iloc[acc_start:idx]
    if len(acc_zone) < 5: return False, {}

    price_change_10d = (silent_row['Close'] / acc_zone['Close'].iloc[0] - 1) * 100
    if price_change_10d < R['max_price_drop_10d']: return False, {}

    first_half_vol = acc_zone['Volume'].iloc[:5].mean()
    second_half_vol = acc_zone['Volume'].iloc[5:].mean()
    if second_half_vol < first_half_vol * R['min_vol_growth']: return False, {}

    green_vol = acc_zone[acc_zone['Close'] > acc_zone['Open']]['Volume'].sum()
    red_vol = acc_zone[acc_zone['Close'] < acc_zone['Open']]['Volume'].sum()
    green_red_ratio = green_vol / red_vol if red_vol > 0 else 99
    if green_red_ratio < R['min_green_red_ratio']: return False, {}

    entry_price = high_max_10d_silent
    sl_price = entry_price * (1 - R['sl_pct'] / 100)

    return True, {
        'Date': df.index[idx].strftime('%Y-%m-%d'),
        'Stock': '',
        'Entry': round(entry_price, 2),
        'SL': round(sl_price, 2)
    }

def is_score_allowed(score):
    for min_score, max_score in R['score_ranges']:
        if min_score <= score <= max_score:
            return True
    return False

def check_final_signal(df, idx):
    silent_row = df.iloc[idx]
    vol_max_10d_silent = silent_row['Vol_10D_Max']
    high_max_10d_silent = silent_row['High_10D_Max']
    if pd.isna(vol_max_10d_silent) or pd.isna(high_max_10d_silent): return None

    pullback_depth = ((high_max_10d_silent - df['Low'].iloc[idx-1]) / high_max_10d_silent) * 100
    year_high = df['High'].iloc[max(0, idx-252):idx].max()
    entry_price = high_max_10d_silent
    nearness_52w = ((year_high - entry_price) / year_high) * 100

    vol_score = (silent_row['Volume'] / vol_max_10d_silent * 40)
    depth_score = (pullback_depth * 3)
    near_score = (max(0, 20-nearness_52w) * 1.5)
    score = vol_score + depth_score + near_score

    if not is_score_allowed(score): return None

    sl_price = entry_price * (1 - R['sl_pct'] / 100)
    target_price = entry_price * (1 + R['target_pct'] / 100)

    return {
        'Stock': '', 'Signal_Date': df.index[idx].strftime('%Y-%m-%d'), 'Regime': regime,
        'Entry': round(entry_price, 2), 'SL': round(sl_price, 2), 'Target': round(target_price, 2),
        'RR': round(R['target_pct'] / R['sl_pct'], 2), 'Score': round(score, 1),
        'CMP': round(silent_row['Close'], 2),
        'Expiry_Date': (df.index[idx] + timedelta(days=10)).strftime('%Y-%m-%d'), 'Status': 'ACTIVE',
    }

# ===== MAIN SCAN - 10 DIN KA BACKFILL =====
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]

uptrend_list = []
silent_list = []
final_signals = []
climax_list = [] # NAYA LIST

print(f"Scanning {len(stocks)} stocks for {lookback_days} days...", flush=True)

for i, stock in enumerate(stocks):
    try:
        if i % 50 == 0:
            print(f"Progress: {i}/{len(stocks)}", flush=True)

        df = yf.download(f"{stock}.NS", start=start_date, end=end_date + timedelta(days=1),
                        progress=False, auto_adjust=True, timeout=10)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 252 or df['Close'].isna().all():
            continue

        df = add_indicators(df)

        # PICHLE 10 TRADING DAYS CHECK KARO
        for day_offset in range(lookback_days):
            idx = len(df) - 1 - day_offset
            if idx < 252: continue

            if not check_liquidity(df, idx): continue

            is_up, uptrend_data = check_uptrend(df, idx)
            if not is_up: continue

            # === CLIMAX LOGIC ===
            # 1. Pichle 60 din me Buying Climax dhoondo
            bc_found = False
            bc_idx = -1
            for j in range(idx - R['sc_lookback'], idx - R['sc_gap_days']):
                if j < 0: continue
                if check_buying_climax(df, j):
                    bc_found = True
                    bc_idx = j
                    break # Latest BC le lo

            # 2. Agar BC mila aur aaj SC bana
            if bc_found:
                is_sc, sc_data = check_selling_climax(df, idx, bc_idx)
                if is_sc:
                    sc_data['Stock'] = stock
                    climax_list.append(sc_data)
                    continue # SC mil gaya to baaki CTD check mat karo

            # === PURANA CTD LOGIC ===
            is_silent, silent_data = check_silent(df, idx)
            signal = check_final_signal(df, idx) if is_silent else None

            if signal:
                signal['Stock'] = stock
                final_signals.append(signal)
            elif is_silent:
                silent_data['Stock'] = stock
                silent_list.append(silent_data)
            else:
                uptrend_list.append({'Stock': stock, **uptrend_data})

        time.sleep(0.2)
    except Exception as e:
        continue

# ===== UPDATE 4 SHEETS =====
def update_sheet_final(sheet_name, data_list, date_col='Date'):
    try:
        ws = sh.worksheet(sheet_name)
    except:
        ws = sh.add_worksheet(title=sheet_name, rows=5000, cols=20)

    ws.clear()
    if data_list:
        df_out = pd.DataFrame(data_list)
        df_out = df_out.drop_duplicates(subset=['Stock', date_col], keep='last')
        df_out = df_out.sort_values([date_col, 'Stock'], ascending=[False, True])
        payload = [df_out.columns.values.tolist()] + df_out.values.tolist()
        ws.update('A1', payload)
        return len(df_out)
    else:
        ws.update('A1', [[f"No data for last {lookback_days} trading days"]])
        return 0

count1 = update_sheet_final('UPTREND_STOCKS', uptrend_list, 'Date')
count2 = update_sheet_final('SILENT_CANDIDATES', silent_list, 'Date')
count3 = update_sheet_final('ACTIVE_SIGNALS', final_signals, 'Signal_Date')
count4 = update_sheet_final('CLIMAX_CANDIDATES', climax_list, 'Date') # NAYA SHEET

print(f"\n=== DONE V15.21 ===", flush=True)
print(f"UPTREND_STOCKS: {count1}", flush=True)
print(f"SILENT_CANDIDATES: {count2}", flush=True)
print(f"ACTIVE_SIGNALS: {count3}", flush=True)
print(f"CLIMAX_CANDIDATES: {count4} - SC after BC", flush=True)
