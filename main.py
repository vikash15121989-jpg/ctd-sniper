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

print("=== V15.8 1 YEAR FAST BACKTEST ===", flush=True)

# 1. SETUP
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

date_raw = str(ws_watchlist.acell('A1').value).split(' ')[0]
date_formats = ['%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%m/%d/%Y']
ref_date = None
for fmt in date_formats:
    try:
        ref_date = datetime.strptime(date_raw, fmt)
        break
    except ValueError:
        continue

# Nifty check
nifty_df = yf.download("^NSEI", period="5y", progress=False, auto_adjust=True)
if ref_date.date() > nifty_df.index[-1].date():
    ref_date = nifty_df.index[-1].to_pydatetime()

# 1 SAAL KA WINDOW
start_date = ref_date - timedelta(days=365)
print(f"Scan Range: {start_date.date()} to {ref_date.date()}", flush=True)

# 2. RULES
R = {
    'min_price': 50,
    'min_daily_value_cr': 0.5,
    'min_vol_shares': 100000,
    'uptrend_days': 10,
    'vol_ma_days': 20,
    'scan_window': 60,
    'silent_lookback': 10,
    'watchlist_days': 10,
    'target_pct': 15,
    'sl_pct': 8,
    'hold_days': 30,
    'checks_per_stock': 1,
    'gap_between_checks': 60,
    # ACCUMULATION FILTERS
    'accum_days': 10,
    'max_price_drop_10d': -2.0,
    'min_vol_growth': 0.9,
    'min_green_red_ratio': 1.2,
}

fail_log = {'Liquidity': 0, 'Data': 0, 'No_Uptrend': 0, 'No_Pullback': 0, 'No_Silent': 0, 'No_Entry': 0, 'Distribution': 0}

def add_indicators(df):
    df['Vol_10D_Max'] = df['Volume'].rolling(10).max().shift(1)
    df['High_10D_Max'] = df['High'].rolling(10).max().shift(1)
    df['Vol_20MA'] = df['Volume'].rolling(20).mean()
    df['Daily_Value'] = df['Close'] * df['Volume']
    df['Daily_Value_20MA'] = df['Daily_Value'].rolling(20).mean()
    df['New_High_10D'] = df['High'] > df['High'].shift(1).rolling(10).max()
    return df

def check_liquidity(df):
    try:
        close = df['Close'].iloc[-1]
        vol_20ma = df['Vol_20MA'].iloc[-1]
        daily_val = df['Daily_Value_20MA'].iloc[-1]
        if pd.isna(close) or close < R['min_price']: return False
        if pd.isna(daily_val) or daily_val < R['min_daily_value_cr'] * 1e7: return False
        if pd.isna(vol_20ma) or vol_20ma < R['min_vol_shares']: return False
        return True
    except:
        return False

def find_pullback_silent(df, end_idx):
    try:
        start_idx = max(0, end_idx - R['scan_window'] + 1)
        for i in range(start_idx, end_idx + 1):
            if i < 252: continue

            # UPTREND CHECK
            uptrend_start = i - R['uptrend_days']
            if uptrend_start < 0: continue
            uptrend_zone = df.iloc[uptrend_start:i]
            new_highs = uptrend_zone['New_High_10D'].sum()
            avg_vol = uptrend_zone['Volume'].mean()
            vol_20ma = df['Vol_20MA'].iloc[i]
            if pd.isna(vol_20ma): continue
            uptrend = new_highs >= 3 and avg_vol > vol_20ma
            if not uptrend: continue

            # PULLBACK MARK
            row = df.iloc[i]
            vol_max_10d = row['Vol_10D_Max']
            high_max_10d = row['High_10D_Max']
            if pd.isna(vol_max_10d) or pd.isna(high_max_10d): continue
            pullback_cond1 = row['High'] < high_max_10d
            pullback_cond2 = row['Volume'] < vol_max_10d
            if not (pullback_cond1 and pullback_cond2): continue

            pullback_idx = i
            search_end = min(pullback_idx + 1 + R['watchlist_days'], len(df))

            for j in range(pullback_idx + 1, search_end):
                silent_row = df.iloc[j]
                vol_max_10d_silent = silent_row['Vol_10D_Max']
                high_max_10d_silent = silent_row['High_10D_Max']
                if pd.isna(vol_max_10d_silent) or pd.isna(high_max_10d_silent): continue

                if silent_row['High'] > high_max_10d_silent and silent_row['Volume'] < vol_max_10d_silent:
                    continue

                silent_cond1 = silent_row['Volume'] > vol_max_10d_silent
                silent_cond2 = silent_row['High'] < high_max_10d_silent

                if silent_cond1 and silent_cond2:

                    # ACCUMULATION FILTER
                    acc_start = max(0, j - R['accum_days'])
                    acc_zone = df.iloc[acc_start:j]
                    if len(acc_zone) < 5: continue

                    price_change_10d = (silent_row['Close'] / acc_zone['Close'].iloc[0] - 1) * 100
                    if price_change_10d < R['max_price_drop_10d']:
                        fail_log['Distribution'] += 1
                        continue

                    first_half_vol = acc_zone['Volume'].iloc[:5].mean()
                    second_half_vol = acc_zone['Volume'].iloc[5:].mean()
                    if second_half_vol < first_half_vol * R['min_vol_growth']:
                        fail_log['Distribution'] += 1
                        continue

                    green_vol = acc_zone[acc_zone['Close'] > acc_zone['Open']]['Volume'].sum()
                    red_vol = acc_zone[acc_zone['Close'] < acc_zone['Open']]['Volume'].sum()
                    green_red_ratio = green_vol / red_vol if red_vol > 0 else 99
                    if green_red_ratio < R['min_green_red_ratio']:
                        fail_log['Distribution'] += 1
                        continue

                    watchlist_date = df.index[j]
                    entry_price = high_max_10d_silent

                    # QUALITY SCORE CALC
                    pullback_depth = ((high_max_10d - row['Low']) / high_max_10d) * 100
                    year_high = df['High'].iloc[max(0, j-252):j].max()
                    nearness_52w = ((year_high - entry_price) / year_high) * 100
                    vol_score = (silent_row['Volume'] / vol_max_10d_silent * 40)
                    depth_score = (pullback_depth * 3)
                    near_score = (max(0, 20-nearness_52w) * 1.5)
                    score = vol_score + depth_score + near_score

                    details = {
                        'uptrend_start_date': df.index[uptrend_start].strftime('%Y-%m-%d'),
                        'pullback_date': df.index[pullback_idx].strftime('%Y-%m-%d'),
                        'pullback_high': round(row['High'], 2),
                        'pullback_vol_ratio': round(row['Volume'] / vol_max_10d, 2),
                        'watchlist_date': watchlist_date.strftime('%Y-%m-%d'),
                        'watchlist_idx': j,
                        'silent_vol': int(silent_row['Volume']),
                        'silent_vol_10d_max': int(vol_max_10d_silent),
                        'silent_vol_ratio': round(silent_row['Volume'] / vol_max_10d_silent, 2),
                        'pullback_depth': round(pullback_depth, 1),
                        'nearness_52w': round(nearness_52w, 1),
                        'quality_score': round(score, 1),
                        'entry_price': round(entry_price, 2),
                        'accum_check': 'PASS',
                        'price_change_10d': round(price_change_10d, 1),
                        'green_red_ratio': round(green_red_ratio, 2),
                        'vol_growth': round(second_half_vol / first_half_vol, 2)
                    }
                    return details, j
        return None, {}
    except:
        return None, {}

def check_entry_in_watchlist(df, watchlist_idx, entry_price):
    try:
        start_idx = watchlist_idx + 1
        if start_idx >= len(df): return False, {}
        end_search = min(start_idx + R['watchlist_days'], len(df))
        window = df.iloc[start_idx:end_search]
        for i in range(len(window)):
            if window['High'].iloc[i] > entry_price:
                entry_idx = start_idx + i
                entry_date = window.index[i]
                sl_price = entry_price * (1 - R['sl_pct']/100)
                target_price = entry_price * (1 + R['target_pct']/100)
                exit_idx = min(entry_idx + R['hold_days'], len(df) - 1)
                exit_price = float(df['Close'].iloc[exit_idx])
                exit_date = df.index[exit_idx]
                result = f'Exit_{R["hold_days"]}D'
                for k in range(entry_idx + 1, exit_idx + 1):
                    h, l = df['High'].iloc[k], df['Low'].iloc[k]
                    if l <= sl_price:
                        exit_price = sl_price
                        exit_date = df.index[k]
                        result = f'SL -{R["sl_pct"]}%'
                        break
                    if h >= target_price:
                        exit_price = target_price
                        exit_date = df.index[k]
                        result = f'Target +{R["target_pct"]}%'
                        break
                pl_pct = ((exit_price - entry_price) / entry_price) * 100
                hold_days = (exit_date - entry_date).days
                return True, {
                    'entry_date': entry_date.strftime('%Y-%m-%d'),
                    'exit_date': exit_date.strftime('%Y-%m-%d'),
                    'exit_price': round(exit_price, 2),
                    'hold_days': int(hold_days),
                    'pl_pct': round(pl_pct, 2),
                    'result': result
                }
        return False, {}
    except:
        return False, {}

def backtest_stock_pullback_silent(df_daily, ticker, year_start, year_end):
    df_daily = add_indicators(df_daily)
    if not check_liquidity(df_daily):
        fail_log['Liquidity'] += 1
        return []

    # 1 SAAL KA DATA HI FILTER KARO
    df_year = df_daily[(df_daily.index >= year_start) & (df_daily.index <= year_end)]
    if len(df_year) < 50: return []

    trades = []
    total_len = len(df_daily)
    if total_len < 252:
        fail_log['Data'] += 1
        return []

    # Sirf year ke andar scan karo
    for check_end_idx in range(total_len-1, 251, -1):
        if df_daily.index[check_end_idx] < year_start: break
        if df_daily.index[check_end_idx] > year_end: continue

        details, watchlist_idx = find_pullback_silent(df_daily, check_end_idx)
        if details is None:
            fail_log['No_Silent'] += 1
            continue
        entry_ok, trade_details = check_entry_in_watchlist(df_daily, watchlist_idx, details['entry_price'])
        if not entry_ok:
            fail_log['No_Entry'] += 1
            continue
        trades.append({'Stock': ticker, **details, **trade_details})
        break # 1 stock = 1 trade max

    return trades

# MAIN LOOP - 1 YEAR ONLY
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
signals = []

print(f"Scanning {len(stocks)} stocks for 1 YEAR only...", flush=True)

for i, stock in enumerate(stocks):
    try:
        if i % 50 == 0:
            print(f"Progress: {i}/{len(stocks)} | Found: {len(signals)} | Distribution Skip: {fail_log['Distribution']}", flush=True)

        # Data download: 365 din + 400 din extra for indicators
        download_start = start_date - timedelta(days=400)
        df = yf.download(f"{stock}.NS", start=download_start, end=ref_date + timedelta(days=1),
                        progress=False, auto_adjust=True, timeout=10)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 252:
            fail_log['Data'] += 1
            continue

        trades = backtest_stock_pullback_silent(df, stock, start_date, ref_date)
        signals.extend(trades)
        time.sleep(0.2)
    except:
        continue

print(f"\nScan Complete. Total Signals: {len(signals)}", flush=True)
print(f"Fail Log: {fail_log}", flush=True)

# OUTPUT
try:
    ws_output = sh.worksheet("1Year_Fast")
except:
    ws_output = sh.add_worksheet(title="1Year_Fast", rows=2000, cols=35)

ws_output.clear()
if signals:
    df_out = pd.DataFrame(signals)
    df_out = df_out.sort_values('quality_score', ascending=False)

    def convert_to_native(val):
        if isinstance(val, (np.integer, np.int64)): return int(val)
        elif isinstance(val, (np.floating, np.float64)): return float(val)
        else: return val
    df_out = df_out.applymap(convert_to_native)

    payload = [df_out.columns.values.tolist()] + df_out.values.tolist()
    ws_output.update('A1', payload)

    # BASIC STATS
    total_trades = len(df_out)
    win_trades = (df_out['pl_pct'] > 0).sum()
    win_rate = round(win_trades / total_trades * 100, 1)
    total_pl = round(df_out['pl_pct'].sum(), 2)
    avg_pl = round(df_out['pl_pct'].mean(), 1)

    # SCORE BUCKET ANALYSIS
    bins = [0, 60, 70, 80, 90, 100]
    labels = ['0-60 Low', '60-70 Avg', '70-80 Good', '80-90 VGood', '90-100 Best']
    df_out['Score_Bucket'] = pd.cut(df_out['quality_score'], bins=bins, labels=labels, include_lowest=True)

    score_analysis = df_out.groupby('Score_Bucket').agg({
        'Stock': 'count',
        'pl_pct': ['sum', 'mean', 'median', lambda x: (x > 0).sum()]
    }).round(2)
    score_analysis.columns = ['Trades', 'Total_PL', 'Avg_PL', 'Median_PL', 'Wins']
    score_analysis['Win_Rate'] = (score_analysis['Wins'] / score_analysis['Trades'] * 100).round(1)
    score_analysis = score_analysis.drop('Wins', axis=1)

    # OUTPUT TO SHEET
    current_row = len(payload) + 3
    ws_output.update(f'A{current_row}', [[f'1 YEAR STATS: {start_date.date()} to {ref_date.date()}']])
    ws_output.update(f'A{current_row+1}', [['Total Trades', total_trades], ['Win Rate %', win_rate],
                                           ['Total P&L %', total_pl], ['Avg P&L %', avg_pl],
                                           ['Distribution Skipped', fail_log['Distribution']]])

    current_row += 7
    ws_output.update(f'A{current_row}', [['SCORE BUCKET ANALYSIS']])
    ws_output.update(f'A{current_row+1}', [score_analysis.reset_index().columns.values.tolist()] + score_analysis.reset_index().values.tolist())

    print(f"\n=== DONE: {total_trades} SIGNALS | {win_rate}% WIN | {total_pl}% TOTAL ===", flush=True)
    print(f"=== DISTRIBUTION SKIPPED: {fail_log['Distribution']} ===", flush=True)

else:
    ws_output.update('A1', [["No Signals Found"]])
    print("\n=== DONE: 0 SIGNALS ===", flush=True)
